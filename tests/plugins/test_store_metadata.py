"""
Copyright (c) 2015, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import json
from datetime import datetime, timedelta
from textwrap import dedent

from flexmock import flexmock
from osbs.api import OSBS
import osbs.conf
from osbs.exceptions import OsbsResponseException
from atomic_reactor.constants import (PLUGIN_KOJI_UPLOAD_PLUGIN_KEY,
                                      PLUGIN_VERIFY_MEDIA_KEY,
                                      PLUGIN_FETCH_SOURCES_KEY,
                                      PLUGIN_RESOLVE_REMOTE_SOURCE)
from atomic_reactor.inner import BuildResult
from atomic_reactor.plugin import ExitPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_add_help import AddHelpPlugin
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin
from atomic_reactor.plugins.exit_store_metadata import StoreMetadataPlugin
from atomic_reactor.util import LazyGit, ManifestDigest, df_parser, DockerfileImages
import pytest
from tests.constants import (LOCALHOST_REGISTRY, DOCKER0_REGISTRY, TEST_IMAGE, TEST_IMAGE_NAME,
                             INPUT_IMAGE)
from tests.util import add_koji_map_in_workflow, is_string_type

DIGEST1 = "sha256:1da9b9e1c6bf6ab40f1627d76e2ad58e9b2be14351ef4ff1ed3eb4a156138189"
DIGEST2 = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
DIGEST_NOT_USED = "not-used"

pytestmark = pytest.mark.usefixtures('user_params')


def prepare(workflow, docker_registries=None):
    if docker_registries is None:
        docker_registries = (LOCALHOST_REGISTRY, DOCKER0_REGISTRY,)

    def update_annotations_on_build(build_id, annotations):
        pass

    def update_labels_on_build(build_id, labels):
        pass

    flexmock(OSBS, update_annotations_on_build=update_annotations_on_build)
    flexmock(OSBS, update_labels_on_build=update_labels_on_build)
    config_kwargs = {
        'namespace': 'namespace',
        'verify_ssl': True,
        'openshift_url': 'http://example.com/',
        'use_auth': True,
        'conf_file': None,
        'build_json_dir': None
    }
    (flexmock(osbs.conf.Configuration)
     .should_call("__init__")
     .with_args(**config_kwargs))

    workflow.user_params['namespace'] = 'namespace'
    workflow.user_params['pipeline_run_name'] = 'store_metadata_test'

    openshift_map = {
        'url': 'http://example.com/',
        'insecure': False,
        'auth': {'enable': True},
    }
    rcm = {'version': 1, 'openshift': openshift_map}
    workflow.conf.conf = rcm
    add_koji_map_in_workflow(workflow, hub_url='/', root_url='')

    tag_conf = workflow.data.tag_conf
    tag_conf.add_floating_image(TEST_IMAGE)
    tag_conf.add_primary_image("namespace/image:version-release")

    tag_conf.add_unique_image("namespace/image:asd123")

    for docker_registry in docker_registries:
        r = workflow.data.push_conf.add_docker_registry(docker_registry)
        r.digests[TEST_IMAGE_NAME] = ManifestDigest(v1=DIGEST_NOT_USED, v2=DIGEST1)
        r.digests["namespace/image:asd123"] = ManifestDigest(v1=DIGEST_NOT_USED,
                                                             v2=DIGEST2)

    flexmock(workflow.imageutil).should_receive('base_image_inspect').and_return({'Id': '01234567'})
    workflow.build_logs = [
        "a", "b",
    ]
    workflow.source.lg = LazyGit(None, commit="commit")
    flexmock(workflow.source.lg)
    # pylint: disable=no-member
    workflow.source.lg.should_receive("_commit_id").and_return("commit")
    # pylint: enable=no-member


@pytest.mark.parametrize(('br_annotations', 'expected_br_annotations'), (
    (None, None),
    ('spam', '"spam"'),
    (['s', 'p', 'a', 'm'], '["s", "p", "a", "m"]'),
))
@pytest.mark.parametrize(('br_labels', 'expected_br_labels'), (
    (None, None),
    ('bacon', 'bacon'),
    (123, '123'),
))
@pytest.mark.parametrize('koji', [True, False])
@pytest.mark.parametrize(('help_results', 'expected_help_results', 'base_from_scratch'), (
    (None, False, False),
    ({
        'help_file': None,
        'status': AddHelpPlugin.NO_HELP_FILE_FOUND,
    }, None, False),
    ({
        'help_file': 'help.md',
        'status': AddHelpPlugin.HELP_GENERATED,
    }, 'help.md', True),
))
@pytest.mark.parametrize(('verify_media_results', 'expected_media_results'), (
    ([], False),
    (["application/vnd.docker.distribution.manifest.v1+json"],
     ["application/vnd.docker.distribution.manifest.v1+json"]),
))
@pytest.mark.parametrize('remote_sources', [True, False])
def test_metadata_plugin(workflow, source_dir, br_annotations, expected_br_annotations,
                         br_labels, expected_br_labels, koji,
                         help_results, expected_help_results, base_from_scratch,
                         verify_media_results, expected_media_results, remote_sources):
    initial_timestamp = datetime.now()
    prepare(workflow)
    if base_from_scratch:
        df_content = """
FROM fedora
RUN yum install -y python-django
CMD blabla
FROM scratch
RUN yum install -y python"""
    else:
        df_content = """
FROM fedora
RUN yum install -y python-django
CMD blabla"""

    df = df_parser(str(source_dir))
    df.content = df_content
    workflow.data.dockerfile_images = DockerfileImages(df.parent_images)
    for parent in df.parent_images:
        if parent != 'scratch':
            workflow.data.dockerfile_images[parent] = "sha256:spamneggs"
    flexmock(workflow, df_path=df.dockerfile_path)
    workflow.df_dir = str(source_dir)

    workflow.data.prebuild_results = {
        AddHelpPlugin.key: help_results
    }
    remote_source_output = [{'name': 'first', 'url': 'cachito_url_for_first'},
                            {'name': 'second', 'url': 'cachito_url_for_second'}]
    if remote_sources:
        workflow.data.prebuild_results[PLUGIN_RESOLVE_REMOTE_SOURCE] = remote_source_output

    if help_results is not None:
        workflow.data.annotations['help_file'] = help_results['help_file']

    workflow.data.postbuild_results = {
        PostBuildRPMqaPlugin.key: "rpm1\nrpm2",
    }
    workflow.data.exit_results = {
        PLUGIN_VERIFY_MEDIA_KEY: verify_media_results,
    }
    workflow.fs_watcher._data = dict(fs_data=None)

    if br_annotations or br_labels:
        workflow.data.build_result = BuildResult(
            image_id=INPUT_IMAGE,
            annotations={'br_annotations': br_annotations} if br_annotations else None,
            labels={'br_labels': br_labels} if br_labels else None,
        )

    timestamp = (initial_timestamp + timedelta(seconds=3)).isoformat()
    workflow.data.plugins_timestamps = {
        PostBuildRPMqaPlugin.key: timestamp,
    }
    workflow.data.plugins_durations = {
        PostBuildRPMqaPlugin.key: 3.03,
    }
    workflow.data.plugins_errors = {}

    if koji:
        cm_annotations = {'metadata_fragment_key': 'metadata.json',
                          'metadata_fragment': 'configmap/build-1-md'}
        workflow.data.postbuild_results[PLUGIN_KOJI_UPLOAD_PLUGIN_KEY] = cm_annotations
        workflow.data.plugins_timestamps[PLUGIN_KOJI_UPLOAD_PLUGIN_KEY] = timestamp
        workflow.data.plugins_durations[PLUGIN_KOJI_UPLOAD_PLUGIN_KEY] = 3.03

    runner = ExitPluginsRunner(
        workflow,
        [{
            'name': StoreMetadataPlugin.key,
            "args": {
                "url": "http://example.com/"
            }
        }]
    )
    output = runner.run()
    assert StoreMetadataPlugin.key in output
    labels = output[StoreMetadataPlugin.key]["labels"]
    annotations = output[StoreMetadataPlugin.key]["annotations"]
    assert "dockerfile" in annotations
    assert is_string_type(annotations['dockerfile'])
    assert "repositories" in annotations
    assert is_string_type(annotations['repositories'])
    assert "commit_id" in annotations
    assert is_string_type(annotations['commit_id'])

    assert "base-image-id" in annotations
    assert is_string_type(annotations['base-image-id'])
    assert "base-image-name" in annotations
    assert is_string_type(annotations['base-image-name'])
    assert "parent_images" in annotations
    assert is_string_type(annotations['parent_images'])
    if base_from_scratch:
        assert annotations["base-image-name"] == ""
        assert annotations["base-image-id"] == ""
        assert '"scratch": "scratch"' in annotations['parent_images']
    else:
        assert annotations["base-image-name"] ==\
               workflow.data.dockerfile_images.original_base_image
        assert annotations["base-image-id"] != ""

        assert (workflow.data.dockerfile_images.base_image.to_str() in
                annotations['parent_images'])
    assert "image-id" in annotations
    assert is_string_type(annotations['image-id'])
    assert "filesystem" in annotations
    assert "fs_data" in annotations['filesystem']

    if koji:
        assert "metadata_fragment" in annotations
        assert is_string_type(annotations['metadata_fragment'])
        assert "metadata_fragment_key" in annotations
        assert is_string_type(annotations['metadata_fragment_key'])
    else:
        assert "metadata_fragment" not in annotations
        assert "metadata_fragment_key" not in annotations

    assert "digests" in annotations
    assert is_string_type(annotations['digests'])
    digests = json.loads(annotations['digests'])
    expected = [{
        "registry": DOCKER0_REGISTRY,
        "repository": TEST_IMAGE,
        "tag": 'latest',
        "digest": DIGEST_NOT_USED,
        "version": "v1"
    }, {
        "registry": DOCKER0_REGISTRY,
        "repository": TEST_IMAGE,
        "tag": 'latest',
        "digest": DIGEST1,
        "version": "v2"
    }, {
        "registry": DOCKER0_REGISTRY,
        "repository": "namespace/image",
        "tag": 'asd123',
        "digest": DIGEST_NOT_USED,
        "version": "v1"
    }, {
        "registry": DOCKER0_REGISTRY,
        "repository": "namespace/image",
        "tag": 'asd123',
        "digest": DIGEST2,
        "version": "v2"
    }, {
        "registry": LOCALHOST_REGISTRY,
        "repository": TEST_IMAGE,
        "tag": 'latest',
        "digest": DIGEST_NOT_USED,
        "version": "v1"
    }, {
        "registry": LOCALHOST_REGISTRY,
        "repository": TEST_IMAGE,
        "tag": 'latest',
        "digest": DIGEST1,
        "version": "v2"
    }, {
        "registry": LOCALHOST_REGISTRY,
        "repository": "namespace/image",
        "tag": 'asd123',
        "digest": DIGEST_NOT_USED,
        "version": "v1"
    }, {
        "registry": LOCALHOST_REGISTRY,
        "repository": "namespace/image",
        "tag": 'asd123',
        "digest": DIGEST2,
        "version": "v2"
    }]
    assert all(digest in expected for digest in digests)
    assert all(digest in digests for digest in expected)

    assert "plugins-metadata" in annotations
    assert "errors" in annotations["plugins-metadata"]
    assert "durations" in annotations["plugins-metadata"]
    assert "timestamps" in annotations["plugins-metadata"]

    plugins_metadata = json.loads(annotations["plugins-metadata"])
    assert "all_rpm_packages" in plugins_metadata["durations"]

    if br_annotations:
        assert annotations['br_annotations'] == expected_br_annotations
    else:
        assert 'br_annotations' not in annotations

    if br_labels:
        assert labels['br_labels'] == expected_br_labels
    else:
        assert 'br_labels' not in labels

    if expected_help_results is False:
        assert 'help_file' not in annotations
    else:
        assert json.loads(annotations['help_file']) == expected_help_results

    if expected_media_results:
        media_types = expected_media_results
        assert sorted(json.loads(annotations['media-types'])) == sorted(list(set(media_types)))
    else:
        assert 'media-types' not in annotations

    if remote_sources:
        assert 'remote_sources' in annotations
        assert json.dumps(remote_source_output) == annotations['remote_sources']
    else:
        assert 'remote_sources' not in annotations


@pytest.mark.parametrize('image_id', ('c9243f9abf2b', None))
@pytest.mark.parametrize(('br_annotations', 'expected_br_annotations'), (
    (None, None),
    ('spam', '"spam"'),
    (['s', 'p', 'a', 'm'], '["s", "p", "a", "m"]'),
))
@pytest.mark.parametrize(('br_labels', 'expected_br_labels'), (
    (None, None),
    ('bacon', 'bacon'),
    (123, '123'),
))
@pytest.mark.parametrize(('verify_media_results', 'expected_media_results'), (
    ([], False),
    (["application/vnd.docker.distribution.manifest.v1+json"],
     ["application/vnd.docker.distribution.manifest.v1+json"]),
))
def test_metadata_plugin_source(image_id, br_annotations, expected_br_annotations,
                                br_labels, expected_br_labels, verify_media_results,
                                expected_media_results, workflow):
    initial_timestamp = datetime.now()
    prepare(workflow)

    if image_id:
        workflow.data.koji_source_manifest = {'config': {'digest': image_id}}

    sources_for_nvr = 'image_build'
    sources_for_koji_build_id = '12345'
    workflow.data.labels['sources_for_koji_build_id'] = sources_for_koji_build_id
    workflow.data.prebuild_results = {
        PLUGIN_FETCH_SOURCES_KEY: {
            'sources_for_koji_build_id': sources_for_koji_build_id,
            'sources_for_nvr': sources_for_nvr,
            'image_sources_dir': 'source_dir',
        }
    }
    workflow.data.exit_results = {
        PLUGIN_VERIFY_MEDIA_KEY: verify_media_results,
    }
    workflow.fs_watcher._data = dict(fs_data=None)

    if br_annotations or br_labels:
        workflow.data.build_result = BuildResult(
            image_id=INPUT_IMAGE,
            annotations={'br_annotations': br_annotations} if br_annotations else None,
            labels={'br_labels': br_labels} if br_labels else None,
        )

    timestamp = (initial_timestamp + timedelta(seconds=3)).isoformat()
    workflow.data.plugins_timestamps = {
        PLUGIN_FETCH_SOURCES_KEY: timestamp,
    }
    workflow.data.plugins_durations = {
        PLUGIN_FETCH_SOURCES_KEY: 3.03,
    }
    workflow.data.plugins_errors = {}

    runner = ExitPluginsRunner(
        workflow,
        [{
            'name': StoreMetadataPlugin.key,
            "args": {
                "url": "http://example.com/"
            }
        }]
    )
    output = runner.run()
    assert StoreMetadataPlugin.key in output
    labels = output[StoreMetadataPlugin.key]["labels"]
    annotations = output[StoreMetadataPlugin.key]["annotations"]
    assert "repositories" in annotations
    assert is_string_type(annotations['repositories'])
    assert "filesystem" in annotations
    assert "fs_data" in annotations['filesystem']
    assert "image-id" in annotations
    assert is_string_type(annotations['image-id'])
    assert annotations['image-id'] == (image_id if image_id else '')
    assert "digests" in annotations
    assert is_string_type(annotations['digests'])
    digests = json.loads(annotations['digests'])
    expected = [{
        "registry": DOCKER0_REGISTRY,
        "repository": TEST_IMAGE,
        "tag": 'latest',
        "digest": DIGEST_NOT_USED,
        "version": "v1"
    }, {
        "registry": DOCKER0_REGISTRY,
        "repository": TEST_IMAGE,
        "tag": 'latest',
        "digest": DIGEST1,
        "version": "v2"
    }, {
        "registry": DOCKER0_REGISTRY,
        "repository": "namespace/image",
        "tag": 'asd123',
        "digest": DIGEST_NOT_USED,
        "version": "v1"
    }, {
        "registry": DOCKER0_REGISTRY,
        "repository": "namespace/image",
        "tag": 'asd123',
        "digest": DIGEST2,
        "version": "v2"
    }, {
        "registry": LOCALHOST_REGISTRY,
        "repository": TEST_IMAGE,
        "tag": 'latest',
        "digest": DIGEST_NOT_USED,
        "version": "v1"
    }, {
        "registry": LOCALHOST_REGISTRY,
        "repository": TEST_IMAGE,
        "tag": 'latest',
        "digest": DIGEST1,
        "version": "v2"
    }, {
        "registry": LOCALHOST_REGISTRY,
        "repository": "namespace/image",
        "tag": 'asd123',
        "digest": DIGEST_NOT_USED,
        "version": "v1"
    }, {
        "registry": LOCALHOST_REGISTRY,
        "repository": "namespace/image",
        "tag": 'asd123',
        "digest": DIGEST2,
        "version": "v2"
    }]
    assert all(digest in expected for digest in digests)
    assert all(digest in digests for digest in expected)

    assert "plugins-metadata" in annotations
    assert "errors" in annotations["plugins-metadata"]
    assert "durations" in annotations["plugins-metadata"]
    assert "timestamps" in annotations["plugins-metadata"]

    plugins_metadata = json.loads(annotations["plugins-metadata"])
    assert PLUGIN_FETCH_SOURCES_KEY in plugins_metadata["durations"]

    if br_annotations:
        assert annotations['br_annotations'] == expected_br_annotations
    else:
        assert 'br_annotations' not in annotations

    if br_labels:
        assert labels['br_labels'] == expected_br_labels
    else:
        assert 'br_labels' not in labels
    assert 'sources_for_koji_build_id' in labels
    assert labels['sources_for_koji_build_id'] == sources_for_koji_build_id

    if expected_media_results:
        media_types = expected_media_results
        assert sorted(json.loads(annotations['media-types'])) == sorted(list(set(media_types)))
    else:
        assert 'media-types' not in annotations


@pytest.mark.parametrize(('res'), (
    {
        'filesystem-koji-task-id': 'example-fs-taskid',
        'base-image-id': 'foobar',
    },
    {
        'base-image-id': 'foobar'
    },
    {}
))
def test_koji_filesystem_label(res, workflow):
    prepare(workflow)
    if 'filesystem-koji-task-id' in res:
        workflow.data.labels['filesystem-koji-task-id'] = res['filesystem-koji-task-id']
    runner = ExitPluginsRunner(
        workflow,
        [{
            'name': StoreMetadataPlugin.key,
            "args": {
                "url": "http://example.com/"
            }
        }]
    )
    output = runner.run()
    labels = output[StoreMetadataPlugin.key]["labels"]

    if 'filesystem-koji-task-id' in res:
        assert 'filesystem-koji-task-id' in labels
        assert labels['filesystem-koji-task-id'] == 'example-fs-taskid'
    if 'filesystem-koji-task-id' not in res:
        assert 'filesystem-koji-task-id' not in labels


def test_metadata_plugin_rpmqa_failure(workflow, source_dir):
    initial_timestamp = datetime.now()
    prepare(workflow)
    df_content = """
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    df = df_parser(str(source_dir))
    df.content = df_content
    flexmock(workflow, df_path=df.dockerfile_path)
    workflow.df_dir = str(source_dir)

    workflow.data.prebuild_results = {}
    workflow.data.postbuild_results = {
        PostBuildRPMqaPlugin.key: RuntimeError(),
        PLUGIN_KOJI_UPLOAD_PLUGIN_KEY: {'metadata_fragment_key': 'metadata.json',
                                        'metadata_fragment': 'configmap/build-1-md'}
    }
    workflow.data.plugins_timestamps = {
        PostBuildRPMqaPlugin.key: (initial_timestamp + timedelta(seconds=3)).isoformat(),
        PLUGIN_KOJI_UPLOAD_PLUGIN_KEY: (initial_timestamp + timedelta(seconds=3)).isoformat(),
    }
    workflow.data.plugins_durations = {
        PostBuildRPMqaPlugin.key: 3.03,
        PLUGIN_KOJI_UPLOAD_PLUGIN_KEY: 3.03,
    }
    workflow.data.plugins_errors = {
        PostBuildRPMqaPlugin.key: 'foo',
        PLUGIN_KOJI_UPLOAD_PLUGIN_KEY: 'bar',
    }

    runner = ExitPluginsRunner(
        workflow,
        [{
            'name': StoreMetadataPlugin.key,
            "args": {
                "url": "http://example.com/"
            }
        }]
    )
    output = runner.run()
    assert StoreMetadataPlugin.key in output
    annotations = output[StoreMetadataPlugin.key]["annotations"]
    assert "dockerfile" in annotations
    assert "repositories" in annotations
    assert "commit_id" in annotations
    assert "base-image-id" in annotations
    assert "base-image-name" in annotations
    assert "image-id" in annotations
    assert "metadata_fragment" in annotations
    assert "metadata_fragment_key" in annotations
    assert "plugins-metadata" in annotations
    assert "errors" in annotations["plugins-metadata"]
    assert "durations" in annotations["plugins-metadata"]
    assert "timestamps" in annotations["plugins-metadata"]

    plugins_metadata = json.loads(annotations["plugins-metadata"])
    assert "all_rpm_packages" in plugins_metadata["errors"]
    assert "all_rpm_packages" in plugins_metadata["durations"]


def test_exit_before_dockerfile_created(workflow, source_dir):
    prepare(workflow)
    workflow.data.exit_results = {}
    workflow.df_dir = str(source_dir)
    workflow._df_path = None

    runner = ExitPluginsRunner(
        workflow,
        [{
            'name': StoreMetadataPlugin.key,
            "args": {
                "url": "http://example.com/"
            }
        }]
    )
    output = runner.run()
    assert StoreMetadataPlugin.key in output
    annotations = output[StoreMetadataPlugin.key]["annotations"]
    assert annotations["base-image-name"] == ""
    assert annotations["base-image-id"] == ""
    assert annotations["dockerfile"] == ""


def test_store_metadata_fail_update_annotations(workflow, source_dir, caplog):
    prepare(workflow)
    workflow.data.exit_results = {}
    df_content = """
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    df = df_parser(str(source_dir))
    df.content = df_content
    flexmock(workflow, df_path=df.dockerfile_path)
    workflow.df_dir = str(source_dir)

    runner = ExitPluginsRunner(
        workflow,
        [{
            'name': StoreMetadataPlugin.key,
            "args": {
                "url": "http://example.com/"
            }
        }]
    )
    (flexmock(OSBS)
        .should_receive('update_annotations_on_build')
        .and_raise(OsbsResponseException('/', 'failed', 0)))
    with pytest.raises(PluginFailedException):
        runner.run()
    assert 'annotations:' in caplog.text


def test_store_metadata_fail_update_labels(workflow, caplog):
    prepare(workflow)
    workflow.data.labels = {'some-label': 'some-value'}

    runner = ExitPluginsRunner(
        workflow,
        [{
            'name': StoreMetadataPlugin.key,
            "args": {
                "url": "http://example.com/"
            }
        }]
    )
    (flexmock(OSBS)
        .should_receive('update_labels_on_build')
        .and_raise(OsbsResponseException('/', 'failed', 0)))
    with pytest.raises(PluginFailedException):
        runner.run()
    assert 'labels:' in caplog.text


@pytest.mark.parametrize(('docker_registries', 'prefixes'), [
    [[], []],
    [
        [],
        ['spam:8888', ],
    ],
    [
        [],
        ['spam:8888', 'maps:9999'],
    ],
    [
        ['spam:8888'],
        ['spam:8888', ]
    ],
    [
        ['spam:8888', 'maps:9999'],
        ['spam:8888', 'maps:9999']
    ],
    [
        ['bacon:8888'],
        ['spam:8888', 'bacon:8888']
    ],
])
def test_filter_repositories(workflow, source_dir, docker_registries, prefixes):
    prepare(workflow, docker_registries=docker_registries)
    df_content = """
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    df = df_parser(str(source_dir))
    df.content = df_content
    flexmock(workflow, df_path=df.dockerfile_path)
    workflow.df_dir = str(source_dir)

    runner = ExitPluginsRunner(
        workflow,
        [{
            'name': StoreMetadataPlugin.key,
            "args": {
                "url": "http://example.com/"
            }
        }]
    )
    output = runner.run()
    assert StoreMetadataPlugin.key in output
    annotations = output[StoreMetadataPlugin.key]["annotations"]
    repositories = json.loads(annotations['repositories'])
    unique_repositories = repositories['unique']
    primary_repositories = repositories['primary']

    matched = set()
    for prefix in prefixes:
        for repo in unique_repositories:
            if repo.startswith(prefix):
                matched.add(repo)

    assert matched == set(unique_repositories)

    matched = set()
    for prefix in prefixes:
        for repo in primary_repositories:
            if repo.startswith(prefix):
                matched.add(repo)

    assert matched == set(primary_repositories)


@pytest.mark.parametrize('koji_conf', (
    {},
    {'task_annotations_whitelist': []},
    {'task_annotations_whitelist': ['foo']},
    ))
def test_set_koji_annotations_whitelist(workflow, source_dir, koji_conf):
    prepare(workflow)
    if koji_conf is not None:
        workflow.conf.conf['koji'] = koji_conf

    df_content = dedent('''\
        FROM nowhere
        RUN nothing
        CMD cowsay moo
        ''')
    df = df_parser(str(source_dir))
    df.content = df_content
    flexmock(workflow, df_path=df.dockerfile_path)
    workflow.df_dir = str(source_dir)
    runner = ExitPluginsRunner(
        workflow,
        [{
            'name': StoreMetadataPlugin.key,
            "args": {
                "url": "http://example.com/"
            }
        }]
    )
    output = runner.run()
    assert StoreMetadataPlugin.key in output
    annotations = output[StoreMetadataPlugin.key]["annotations"]
    whitelist = None
    if koji_conf:
        whitelist = koji_conf.get('task_annotations_whitelist')

    if whitelist:
        assert 'koji_task_annotations_whitelist' in annotations
        assert all(entry in whitelist for entry in koji_conf['task_annotations_whitelist'])
        assert all(entry in whitelist for entry in json.loads(
            annotations['koji_task_annotations_whitelist']))
    else:
        assert 'koji_task_annotations_whitelist' not in annotations


def test_plugin_annotations(workflow):
    prepare(workflow)
    workflow.data.annotations = {'foo': {'bar': 'baz'}, 'spam': ['eggs']}

    runner = ExitPluginsRunner(
        workflow,
        [{
            'name': StoreMetadataPlugin.key,
            "args": {
                "url": "http://example.com/"
            }
        }]
    )

    output = runner.run()
    annotations = output[StoreMetadataPlugin.key]["annotations"]

    assert annotations['foo'] == '{"bar": "baz"}'
    assert annotations['spam'] == '["eggs"]'


def test_plugin_labels(workflow):
    prepare(workflow)
    workflow.data.labels = {'foo': 1, 'bar': 'two'}

    runner = ExitPluginsRunner(
        workflow,
        [{
            'name': StoreMetadataPlugin.key,
            "args": {
                "url": "http://example.com/"
            }
        }]
    )

    output = runner.run()
    labels = output[StoreMetadataPlugin.key]["labels"]

    assert labels['foo'] == '1'
    assert labels['bar'] == 'two'
