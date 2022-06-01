"""
Copyright (c) 2015-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import inspect
import json
import logging
import os
import signal
import time
from dataclasses import fields, Field
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict, List, Tuple

import pytest
from flexmock import flexmock

import osbs.exceptions
from dockerfile_parse import DockerfileParser
from osbs.utils import ImageName

from atomic_reactor.inner import (BuildResults, BuildResultsEncoder,
                                  BuildResultsJSONDecoder, DockerBuildWorkflow,
                                  FSWatcher, ImageBuildWorkflowData, TagConf)
from atomic_reactor.source import PathSource, DummySource
from atomic_reactor.util import (
    DockerfileImages, validate_with_schema, graceful_chain_get
)
from atomic_reactor.dirs import ContextDir
from atomic_reactor.plugin import Plugin, PluginFailedException
from tests.util import is_string_type
from tests.constants import DOCKERFILE_MULTISTAGE_CUSTOM_BAD_PATH


BUILD_RESULTS_ATTRS = ['build_logs',
                       'built_img_inspect',
                       'built_img_info',
                       'base_img_info',
                       'base_plugins_output',
                       'built_img_plugins_output']

NAMESPACE = 'test-namespace'
PIPELINE_RUN_NAME = 'test-pipeline-run'

pytestmark = pytest.mark.usefixtures('user_params')


class Watcher(object):
    def __init__(self, raise_exc=None):
        self.called = False
        self.raise_exc = raise_exc

    def call(self):
        self.called = True
        if self.raise_exc is not None:
            raise self.raise_exc    # pylint: disable=raising-bad-type

    def was_called(self):
        return self.called


class WatcherWithSignal(Watcher):
    def __init__(self, signal=None):
        super(WatcherWithSignal, self).__init__()
        self.signal = signal

    def call(self):
        super(WatcherWithSignal, self).call()
        if self.signal:
            os.kill(os.getpid(), self.signal)


class UpdateMaintainerPlugin(Plugin):
    key = "update_maintainer"
    def run(self): pass


class PushImagePlugin(Plugin):
    key = "push_image"
    def run(self): pass


class CleanupPlugin(Plugin):
    key = "cleanup"
    def run(self): pass


class WatchedMixIn(object):
    """
    Mix-in class for plugins we want to watch.
    """

    def __init__(self, workflow, watcher, *args, **kwargs):
        super(WatchedMixIn, self).__init__(workflow, *args, **kwargs)
        self.watcher = watcher

    def run(self):
        self.watcher.call()


class UpdateMaintainerPluginWatched(WatchedMixIn, Plugin):
    key = 'update_maintainer_watched'

    def run(self):
        super().run()
        return True


class PushImagePluginWatched(WatchedMixIn, Plugin):
    key = 'push_image_watched'

    def run(self):
        super().run()
        return "pushed image"


class CleanupPluginWatched(WatchedMixIn, Plugin):
    key = 'cleanup_watched'


def test_build_results_encoder():
    results = BuildResults()
    expected_data = {}
    for attr in BUILD_RESULTS_ATTRS:
        setattr(results, attr, attr)
        expected_data[attr] = attr

    data = json.loads(json.dumps(results, cls=BuildResultsEncoder))
    assert data == expected_data


def test_build_results_decoder():
    data = {}
    expected_results = BuildResults()
    for attr in BUILD_RESULTS_ATTRS:
        setattr(expected_results, attr, attr)
        data[attr] = attr

    results = json.loads(json.dumps(data), cls=BuildResultsJSONDecoder)
    for attr in set(BUILD_RESULTS_ATTRS) - {'build_logs'}:
        assert getattr(results, attr) == getattr(expected_results, attr)


@pytest.mark.parametrize(
    'pipeline_status, plugin_errors, expect_outcome',
    [
        (
            {"prev_task_failed": False, "prev_task_cancelled": False},
            {},
            # outcome is (failed, cancelled)
            (False, False),
        ),
        (
            {"prev_task_failed": True, "prev_task_cancelled": False},
            {},
            (True, False),
        ),
        (
            {"prev_task_failed": False, "prev_task_cancelled": True},
            {},
            (True, True),
        ),
        (
            {"prev_task_failed": True, "prev_task_cancelled": True},
            {},
            (True, True),
        ),
        (
            {"prev_task_failed": False, "prev_task_cancelled": False},
            {"some_plugin": "some error"},
            (True, False),
        ),
        (
            {"prev_task_failed": True, "prev_task_cancelled": False},
            {"some_plugin": "some error"},
            (True, False),
        ),
        (
            {"prev_task_failed": False, "prev_task_cancelled": True},
            {"some_plugin": "some error"},
            (True, True),
        ),
        (
            {"prev_task_failed": True, "prev_task_cancelled": True},
            {"some_plugin": "some error"},
            (True, True),
        ),
    ],
)
def test_check_build_outcome(
    workflow: DockerBuildWorkflow,
    pipeline_status: Dict[str, bool],
    plugin_errors: Dict[str, str],
    expect_outcome: Tuple[bool, bool],
):
    prev_cancelled = pipeline_status['prev_task_cancelled']
    prev_failed = pipeline_status['prev_task_failed']

    workflow.conf.conf['openshift'] = {'url': 'https://something.com'}
    mock_osbs = flexmock(workflow.osbs)
    mock_osbs.should_receive('build_has_any_cancelled_tasks').once().and_return(prev_cancelled)
    (mock_osbs
        .should_receive('build_has_any_failed_tasks')
        .times(0 if prev_cancelled else 1)
        .and_return(prev_failed))

    workflow.data.plugins_errors = plugin_errors

    assert workflow.check_build_outcome() == expect_outcome


@pytest.mark.parametrize("terminate_build", [True, False])
def test_workflow_build_image(terminate_build: bool, workflow: DockerBuildWorkflow, caplog):
    """
    Test workflow for base images
    """
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(UpdateMaintainerPlugin)
    watch_update_maintainer = Watcher()
    watch_push_image = Watcher()
    watch_cleanup = Watcher()

    workflow.plugins_conf = [
        {
            'name': UpdateMaintainerPluginWatched.key,
            'args': {'watcher': watch_update_maintainer},
        },
        {
            'name': PushImagePluginWatched.key,
            'args': {'watcher': watch_push_image}
        },
        {
            'name': CleanupPluginWatched.key,
            'args': {'watcher': watch_cleanup}
        },
    ]
    workflow.plugin_files = [this_file]

    # This test does not require a separate FSWatcher thread.
    fs_watcher = flexmock(workflow.fs_watcher)
    fs_watcher.should_receive('start')
    fs_watcher.should_receive('finish')

    if terminate_build:
        workflow.plugins_conf[1]['args']['watcher'] = WatcherWithSignal(signal=signal.SIGTERM)
        workflow.build_docker_image()
        assert "Build was canceled" in caplog.text
        assert workflow.data.build_canceled
    else:
        workflow.build_docker_image()

        assert watch_update_maintainer.was_called()
        assert watch_push_image.was_called()
        assert watch_cleanup.was_called()

        results = workflow.data.plugins_results
        assert results[UpdateMaintainerPluginWatched.key]
        assert "pushed image" == results[PushImagePluginWatched.key]
        assert results[CleanupPluginWatched.key] is None


@pytest.mark.parametrize('plugins_conf', [
    # No such plugin but it is required, subsequent plugin should not run.
    [
        {'name': 'no plugin', 'args': {}},
        {'name': UpdateMaintainerPluginWatched.key, 'args': {'watcher': Watcher()}}
    ],
])
def test_bad_plugins_conf(plugins_conf: List[Dict[str, Any]], workflow, caplog):
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(UpdateMaintainerPlugin)

    caplog.clear()

    workflow.plugins_conf = plugins_conf
    workflow.plugin_files = [this_file]

    # Find the 'watcher' parameter
    watchers = [conf.get('args', {}).get('watcher') for conf in plugins_conf]
    watcher = [x for x in watchers if x][0]

    with pytest.raises(PluginFailedException):
        workflow.build_docker_image()

    assert not watcher.was_called()
    assert workflow.data.plugins_errors
    assert all([is_string_type(plugin)
                for plugin in workflow.data.plugins_errors])
    assert all([is_string_type(reason)
                for reason in workflow.data.plugins_errors.values()])

    assert any(record.levelno == logging.ERROR for record in caplog.records)


@pytest.mark.parametrize('plugins_conf', [
    # No 'args' key
    [
        {'name': UpdateMaintainerPlugin.key},
        {'name': UpdateMaintainerPluginWatched.key, 'args': {'watcher': Watcher()}},
    ],
    # No such plugin, not required, subsequent plugins should run.
    [
        {'name': 'no plugin', 'args': {}, 'required': False},
        {'name': UpdateMaintainerPluginWatched.key, 'args': {'watcher': Watcher()}}
    ],
])
def test_good_plugins_conf(plugins_conf: List[Dict[str, Any]], workflow, caplog):
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(UpdateMaintainerPlugin)

    caplog.clear()

    workflow.plugins_conf = plugins_conf
    workflow.plugin_files = [this_file]

    # Find the 'watcher' parameter
    watchers = [conf.get('args', {}).get('watcher') for conf in plugins_conf]
    watcher = [x for x in watchers if x][0]

    workflow.build_docker_image()
    assert watcher.was_called()
    assert not workflow.data.plugins_errors
    assert all(record.levelno != logging.ERROR for record in caplog.records)


def test_parse_dockerfile_again_after_data_is_loaded(context_dir, build_dir, tmpdir):
    context_dir = ContextDir(Path(tmpdir.join("context_dir")))
    wf_data = ImageBuildWorkflowData.load_from_dir(context_dir)
    # Note that argument source is None, that causes a DummySource is created
    # and "FROM scratch" is included in the Dockerfile.
    workflow = DockerBuildWorkflow(context_dir, build_dir, NAMESPACE, PIPELINE_RUN_NAME, wf_data)
    assert ["scratch"] == workflow.data.dockerfile_images.original_parents

    # Now, save the workflow data and load it again
    wf_data.save(context_dir)

    another_source = DummySource("git", "https://git.host/")
    dfp = DockerfileParser(another_source.source_path)
    dfp.content = 'FROM fedora:35\nCMD ["bash", "--version"]'

    wf_data = ImageBuildWorkflowData.load_from_dir(context_dir)
    flexmock(DockerBuildWorkflow).should_receive("_parse_dockerfile_images").never()
    flexmock(wf_data.dockerfile_images).should_receive("set_source_registry").never()
    workflow = DockerBuildWorkflow(context_dir, build_dir, NAMESPACE, PIPELINE_RUN_NAME, wf_data,
                                   source=another_source)
    assert ["scratch"] == workflow.data.dockerfile_images.original_parents, \
        "The dockerfile_images should not be changed."


@pytest.mark.parametrize('has_version', [True, False])
def test_show_version(has_version, context_dir, build_dir, workflow: DockerBuildWorkflow, caplog):
    """
    Test atomic-reactor print version of osbs-client used to build the build json
    if available
    """
    version = "1.0"
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(UpdateMaintainerPlugin)
    plugin_watcher = Watcher()

    caplog.clear()

    kwargs = {}
    if has_version:
        kwargs['client_version'] = version

    workflow = DockerBuildWorkflow(
        context_dir,
        build_dir,
        namespace=NAMESPACE,
        pipeline_run_name=PIPELINE_RUN_NAME,
        source=None,
        plugins_conf=[
            {'name': UpdateMaintainerPlugin.key, 'args': {'watcher': plugin_watcher}}
        ],
        plugin_files=[this_file],
        **kwargs,
    )

    workflow.build_docker_image()
    expected_log_message = f"build json was built by osbs-client {version}"
    assert any(
        expected_log_message in record.message
        for record in caplog.records
        if record.levelno == logging.DEBUG
    ) == has_version


def test_parent_images_to_str(workflow, caplog):
    workflow.data.dockerfile_images = DockerfileImages(['fedora:latest', 'bacon'])
    workflow.data.dockerfile_images['fedora:latest'] = "spam"
    expected_results = {
        "fedora:latest": "spam:latest"
    }
    assert workflow.parent_images_to_str() == expected_results
    assert "None in: base bacon:latest has parent None" in caplog.text


def test_no_base_image(context_dir, build_dir):
    source = DummySource("git", "https://git.host/")
    dfp = DockerfileParser(source.source_path)
    dfp.content = "# no FROM\nADD spam /eggs"
    with pytest.raises(RuntimeError, match="no base image specified"):
        DockerBuildWorkflow(context_dir,
                            build_dir,
                            namespace=NAMESPACE,
                            pipeline_run_name=PIPELINE_RUN_NAME,
                            source=source)


def test_different_custom_base_images(context_dir, build_dir, source_dir):
    source = PathSource(
        "path", f"file://{DOCKERFILE_MULTISTAGE_CUSTOM_BAD_PATH}", workdir=str(source_dir)
    )
    with pytest.raises(NotImplementedError) as exc:
        DockerBuildWorkflow(context_dir,
                            build_dir,
                            namespace=NAMESPACE,
                            pipeline_run_name=PIPELINE_RUN_NAME,
                            source=source)
    message = "multiple different custom base images aren't allowed in Dockerfile"
    assert message in str(exc.value)


def test_copy_from_unkown_stage(context_dir, build_dir, source_dir):
    """test when user has specified COPY --from=image (instead of builder)"""
    source = PathSource("path", f"file://{source_dir}", workdir=str(source_dir))

    dfp = DockerfileParser(str(source_dir))
    dfp.content = dedent("""\
        FROM monty as vikings
        FROM python
        # using a stage name we haven't seen should break:
        COPY --from=notvikings /spam/eggs /bin/eggs
    """)
    with pytest.raises(RuntimeError) as exc_info:
        DockerBuildWorkflow(context_dir,
                            build_dir,
                            namespace=NAMESPACE,
                            pipeline_run_name=PIPELINE_RUN_NAME,
                            source=source)
    assert "FROM notvikings AS source" in str(exc_info.value)


def test_copy_from_invalid_index(context_dir, build_dir, source_dir):
    source = PathSource("path", f"file://{source_dir}", workdir=str(source_dir))

    dfp = DockerfileParser(str(source_dir))
    dfp.content = dedent("""\
        FROM monty as vikings
        # using an index we haven't seen should break:
        COPY --from=5 /spam/eggs /bin/eggs
    """)
    with pytest.raises(RuntimeError) as exc_info:
        DockerBuildWorkflow(context_dir,
                            build_dir,
                            namespace=NAMESPACE,
                            pipeline_run_name=PIPELINE_RUN_NAME,
                            source=source)
    assert "COPY --from=5" in str(exc_info.value)


def test_fs_watcher_update(monkeypatch):

    # check that using the actual os call does not choke
    assert type(FSWatcher._update({})) is dict

    # check that the data actually gets updated
    stats = flexmock(
        f_frsize=1000,  # pretend blocks are 1000 bytes to make mb come out right
        f_blocks=101 * 1000,
        f_bfree=99 * 1000,
        f_files=1, f_ffree=1,
    )
    data = dict(mb_total=101, mb_free=100)
    monkeypatch.setattr(os, "statvfs", stats)
    assert type(FSWatcher._update(data)) is dict
    assert data["mb_used"] == 2
    assert data["mb_free"] == 99


def test_fs_watcher(monkeypatch):
    w = FSWatcher()
    monkeypatch.setattr(time, "sleep", lambda x: x)  # don't waste a second of test time
    w.start()
    w.finish()
    w.join(0.1)  # timeout if thread still running
    assert not w.is_alive()
    assert "mb_used" in w.get_usage_data()


class TestTagConf:
    """Test class TagConf"""

    def test_dump_empty_object(self):
        expected = {
            'primary_images': [],
            'unique_images': [],
            'floating_images': [],
        }
        assert expected == TagConf().as_dict()

    def test_as_dict(self):
        tag_conf = TagConf()
        tag_conf.add_primary_image('r.fp.o/f:35')
        tag_conf.add_floating_image('ns/img:latest')
        tag_conf.add_floating_image('ns1/img2:devel')
        expected = {
            'primary_images': [ImageName.parse('r.fp.o/f:35')],
            'unique_images': [],
            'floating_images': [
                ImageName.parse('ns/img:latest'),
                ImageName.parse('ns1/img2:devel'),
            ],
        }
        assert expected == tag_conf.as_dict()

    @pytest.mark.parametrize(
        'input_data,expected_primary_images,expected_unique_images,expected_floating_images',
        [
            [
                {
                    'primary_images': ['registry/image:2.4'],
                    'unique_images': ['registry/image:2.4'],
                    'floating_images': ['registry/image:latest'],
                },
                [ImageName.parse('registry/image:2.4')],
                [ImageName.parse('registry/image:2.4')],
                [ImageName.parse('registry/image:latest')],
            ],
            [
                {
                    'primary_images': [],
                    'unique_images': [],
                    'floating_images': ['registry/image:latest', 'registry/image:devel'],
                },
                [],
                [],
                [
                    ImageName.parse('registry/image:latest'),
                    ImageName.parse('registry/image:devel'),
                ],
            ],
            [
                {'floating_images': ['registry/image:latest']},
                [],
                [],
                [ImageName.parse('registry/image:latest')],
            ],
        ],
    )
    def test_parse_images(
        self, input_data, expected_primary_images, expected_unique_images, expected_floating_images
    ):
        tag_conf = TagConf.load(input_data)
        assert expected_primary_images == tag_conf.primary_images
        assert expected_unique_images == tag_conf.unique_images
        assert expected_floating_images == tag_conf.floating_images

    def test_get_unique_images_with_platform(self):
        image = 'registry.com/org/hello:world-16111-20220103213046'
        platform = 'x86_64'

        tag_conf = TagConf()
        tag_conf.add_unique_image(image)

        expected = [ImageName.parse(f'{image}-{platform}')]
        actual = tag_conf.get_unique_images_with_platform(platform)

        assert actual == expected


class TestWorkflowData:
    """Test class ImageBuildWorkflowData."""

    def test_creation(self):
        data = ImageBuildWorkflowData()
        assert data.dockerfile_images.is_empty
        assert data.tag_conf.is_empty
        assert {} == data.plugins_results

    def test_load_from_empty_dump(self):
        wf_data = ImageBuildWorkflowData.load({})
        empty_data = ImageBuildWorkflowData()
        field: Field
        for field in fields(ImageBuildWorkflowData):
            name = field.name
            assert getattr(empty_data, name) == getattr(wf_data, name)

    def test_load_from_dump(self):
        input_data = {
            "dockerfile_images": {
                "original_parents": ["scratch"],
                "local_parents": [],
                "source_registry": None,
                "organization": None,
            },
            "plugins_results": {"plugin_1": "result"},
            "tag_conf": {
                "floating_images": [
                    ImageName.parse("registry/httpd:2.4").to_str(),
                ],
            },
        }
        wf_data = ImageBuildWorkflowData.load(input_data)

        expected_df_images = DockerfileImages.load(input_data["dockerfile_images"])
        assert expected_df_images == wf_data.dockerfile_images
        assert input_data["plugins_results"] == wf_data.plugins_results
        assert TagConf.load(input_data["tag_conf"]) == wf_data.tag_conf

    def test_load_from_empty_directory(self, tmpdir):
        context_dir = tmpdir.join("context_dir").mkdir()
        # Note: no data file is created here, e.g. workflow.json.
        wf_data = ImageBuildWorkflowData.load_from_dir(ContextDir(context_dir))
        assert wf_data.dockerfile_images.is_empty
        assert wf_data.tag_conf.is_empty
        assert {} == wf_data.plugins_results

    @pytest.mark.parametrize("data_path,prop_name,wrong_value", [
        # digests should map to an object rather than a string
        [["tag_conf"], "original_parents", "wrong value"],
        # tag name should map to an object rather than a string
        [["tag_conf"], "floating_images", "wrong value"],
    ])
    def test_load_invalid_data_from_directory(self, data_path, prop_name, wrong_value, tmpdir):
        """Test the workflow data is validated by JSON schema when reading from context_dir."""
        context_dir = ContextDir(Path(tmpdir.join("context_dir").mkdir()))

        data = ImageBuildWorkflowData(dockerfile_images=DockerfileImages(["scratch"]))
        data.tag_conf.add_floating_image("registry/httpd:2.4")
        data.plugins_results["plugin_1"] = "result"
        data.save(context_dir)

        saved_data = json.loads(context_dir.workflow_json.read_bytes())
        # Make data invalid
        graceful_chain_get(saved_data, *data_path, make_copy=False)[prop_name] = wrong_value
        context_dir.workflow_json.write_text(json.dumps(saved_data), encoding="utf-8")

        with pytest.raises(osbs.exceptions.OsbsValidationException):
            ImageBuildWorkflowData.load_from_dir(context_dir)

    def test_save_and_load(self, tmpdir):
        """Test save workflow data and then load them back properly."""
        tag_conf = TagConf()
        tag_conf.add_floating_image(ImageName.parse("registry/image:latest"))
        tag_conf.add_primary_image(ImageName.parse("registry/image:1.0"))

        wf_data = ImageBuildWorkflowData(
            dockerfile_images=DockerfileImages(["scratch", "registry/f:35"]),
            # Test object in dict values is serialized
            tag_conf=tag_conf,
            plugins_results={
                "plugin_a": {
                    'parent-images-koji-builds': {
                        ImageName(repo='base', tag='latest').to_str(): {
                            'id': 123456789,
                            'nvr': 'base-image-1.0-99',
                            'state': 1,
                        },
                    },
                },
                "tag_and_push": [
                    # Such object in a list should be handled properly.
                    ImageName(registry="localhost:5000", repo='image', tag='latest'),
                ],
                "image_build": {"logs": ["Build succeeds."]},
            },
            koji_upload_files=[
                {
                    "local_filename": "/path/to/build1.log",
                    "dest_filename": "x86_64-build.log",
                },
                {
                    "local_filename": "/path/to/dir1/remote-source.tar.gz",
                    "dest_filename": "remote-source.tar.gz",
                },
            ]
        )

        context_dir = ContextDir(Path(tmpdir.join("context_dir").mkdir()))
        wf_data.save(context_dir)

        assert context_dir.workflow_json.exists()

        # Verify the saved data matches the schema
        saved_data = json.loads(context_dir.workflow_json.read_bytes())
        try:
            validate_with_schema(saved_data, "schemas/workflow_data.json")
        except osbs.exceptions.OsbsValidationException as e:
            pytest.fail(f"The dumped workflow data does not match JSON schema: {e}")

        # Load and verify the loaded data
        loaded_wf_data = ImageBuildWorkflowData.load_from_dir(context_dir)

        assert wf_data.dockerfile_images == loaded_wf_data.dockerfile_images
        assert wf_data.tag_conf == loaded_wf_data.tag_conf
        assert wf_data.plugins_results == loaded_wf_data.plugins_results
