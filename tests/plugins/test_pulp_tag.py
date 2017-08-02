"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import os
import sys

from osbs.build.build_response import BuildResponse
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from atomic_reactor.util import ImageName
from atomic_reactor.build import BuildResult
from atomic_reactor.plugins.build_orchestrate_build import (OrchestrateBuildPlugin,
                                                            WORKSPACE_KEY_BUILD_INFO)
from atomic_reactor.constants import PLUGIN_PULP_TAG_KEY

try:
    if sys.version_info.major > 2:
        # importing dockpulp in Python 3 causes SyntaxError
        raise ImportError

    import dockpulp
except (ImportError):
    dockpulp = None

import pytest
from flexmock import flexmock
from tests.constants import INPUT_IMAGE, SOURCE, MOCK
if MOCK:
    from tests.docker_mock import mock_docker


class X(object):
    image_id = INPUT_IMAGE
    git_dockerfile_path = None
    git_path = None
    base_image = ImageName(repo="qwe", tag="asd")


class BuildInfo(object):
    def __init__(self, v1_image_id=None):
        annotations = {'meta': 'test'}
        if v1_image_id:
            annotations['v1-image-id'] = v1_image_id

        self.build = BuildResponse({'metadata': {'annotations': annotations}})


def prepare(v1_image_ids={}):
    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)
    setattr(workflow, 'tag_conf', X())
    setattr(workflow.tag_conf, 'images', [ImageName(repo="image-name1")])

    # Mock dockpulp and docker
    dockpulp.Pulp = flexmock(dockpulp.Pulp)
    dockpulp.Pulp.registry = 'registry.example.com'
    (flexmock(dockpulp.imgutils).should_receive('get_metadata')
     .with_args(object)
     .and_return([{'id': 'foo'}]))
    (flexmock(dockpulp.imgutils).should_receive('get_manifest')
     .with_args(object)
     .and_return([{'id': 'foo'}]))
    (flexmock(dockpulp.imgutils).should_receive('get_versions')
     .with_args(object)
     .and_return({'id': '1.6.0'}))
    (flexmock(dockpulp.imgutils).should_receive('check_repo')
     .and_return(0))
    (flexmock(dockpulp.Pulp)
     .should_receive('set_certs')
     .with_args(object, object))
    (flexmock(dockpulp.Pulp)
     .should_receive('getRepos')
     .with_args(list, fields=list)
     .and_return([
         {"id": "redhat-image-name1"},
         {"id": "redhat-prefix-image-name2"}
      ]))
    (flexmock(dockpulp.Pulp)
     .should_receive('createRepo'))
    (flexmock(dockpulp.Pulp)
     .should_receive('upload')
     .with_args(unicode)).at_most().once()
    (flexmock(dockpulp.Pulp)
     .should_receive('copy')
     .with_args(unicode, unicode))
    (flexmock(dockpulp.Pulp)
     .should_receive('updateRepo')
     .with_args('redhat-image-name1', {'tag': 'latest:ppc64le_v1_image_id'}))
    (flexmock(dockpulp.Pulp)
     .should_receive('crane')
     .with_args(list, wait=True)
     .and_return([2, 3, 4]))
    (flexmock(dockpulp.Pulp)
     .should_receive('')
     .with_args(object, object)
     .and_return([1, 2, 3]))
    (flexmock(dockpulp.Pulp)
     .should_receive('watch_tasks')
     .with_args(list))

    annotations = {
        'worker-builds': {
            'x86_64': {
                'build': {
                    'build-name': 'build-1-x64_64',
                },
                'metadata_fragment': 'configmap/build-1-x86_64-md',
                'metadata_fragment_key': 'metadata.json',
            },
            'ppc64le': {
                'build': {
                    'build-name': 'build-1-ppc64le',
                },
                'metadata_fragment': 'configmap/build-1-ppc64le-md',
                'metadata_fragment_key': 'metadata.json',
            }
        }
    }

    workflow.build_result = BuildResult(logs=["docker build log - \u2018 \u2017 \u2019 \n'"],
                                        image_id="id1234", annotations=annotations)
    build_info = {}
    build_info['x86_64'] = BuildInfo()
    build_info['ppc64le'] = BuildInfo()

    for platform, v1_image_id in v1_image_ids.items():
        build_info[platform] = BuildInfo(v1_image_id)

    workflow.plugin_workspace = {
        OrchestrateBuildPlugin.key: {
            WORKSPACE_KEY_BUILD_INFO: build_info
        }
    }

    mock_docker()
    return tasker, workflow


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
@pytest.mark.parametrize(("v1_image_ids", "should_raise"), [
    ({'x86_64': None, 'ppc64le': None}, False),
    ({'x86_64': None, 'ppc64le': 'ppc64le_v1_image_id'}, False),
    ({'x86_64': 'ppc64le_v1_image_id', 'ppc64le': 'ppc64le_v1_image_id'}, True),
])
def test_pulp_tag_basic(tmpdir, monkeypatch, v1_image_ids, should_raise, caplog):
    tasker, workflow = prepare(v1_image_ids)
    monkeypatch.setenv('SOURCE_SECRET_PATH', str(tmpdir))
    with open(os.path.join(str(tmpdir), "pulp.cer"), "wt") as cer:
        cer.write("pulp certificate\n")
    with open(os.path.join(str(tmpdir), "pulp.key"), "wt") as key:
        key.write("pulp key\n")

    runner = PostBuildPluginsRunner(tasker, workflow, [{
        'name': PLUGIN_PULP_TAG_KEY,
        'args': {
            'pulp_registry_name': 'test'
        }}])

    if should_raise:
        with pytest.raises(PluginFailedException):
            runner.run()
        return

    msg = None
    expected_results = {}
    for platform, v1_image_id in v1_image_ids.items():
        if v1_image_id:
            msg = "tagging v1-image-id ppc64le_v1_image_id for platform ppc64le"
            expected_results = {
                'redhat-image-name1': {'tag': 'latest:ppc64le_v1_image_id'},
            }
            break

    results = runner.run()
    if msg:
        assert msg in caplog.text()
    else:
        assert "tagging v1-image-id" not in caplog.text()
    assert results['pulp_tag'] == expected_results


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
def test_pulp_tag_source_secret(tmpdir, monkeypatch, caplog):
    v1_image_ids = {'x86_64': None,
                    'ppc64le': 'ppc64le_v1_image_id'}
    msg = "tagging v1-image-id ppc64le_v1_image_id for platform ppc64le"
    expected_results = {
        'redhat-image-name1': {'tag': 'latest:ppc64le_v1_image_id'},
    }

    tasker, workflow = prepare(v1_image_ids)
    monkeypatch.setenv('SOURCE_SECRET_PATH', str(tmpdir))
    with open(os.path.join(str(tmpdir), "pulp.cer"), "wt") as cer:
        cer.write("pulp certificate\n")
    with open(os.path.join(str(tmpdir), "pulp.key"), "wt") as key:
        key.write("pulp key\n")

    runner = PostBuildPluginsRunner(tasker, workflow, [{
        'name': PLUGIN_PULP_TAG_KEY,
        'args': {
            'pulp_registry_name': 'test'
        }}])

    results = runner.run()
    assert msg in caplog.text()
    assert results['pulp_tag'] == expected_results


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
def test_pulp_tag_service_account_secret(tmpdir, monkeypatch, caplog):
    v1_image_ids = {'x86_64': None,
                    'ppc64le': 'ppc64le_v1_image_id'}
    msg = "tagging v1-image-id ppc64le_v1_image_id for platform ppc64le"
    expected_results = {
        'redhat-image-name1': {'tag': 'latest:ppc64le_v1_image_id'},
    }

    tasker, workflow = prepare(v1_image_ids)
    monkeypatch.setenv('SOURCE_SECRET_PATH', str(tmpdir) + "/not-used")
    with open(os.path.join(str(tmpdir), "pulp.cer"), "wt") as cer:
        cer.write("pulp certificate\n")
    with open(os.path.join(str(tmpdir), "pulp.key"), "wt") as key:
        key.write("pulp key\n")

    runner = PostBuildPluginsRunner(tasker, workflow, [{
        'name': PLUGIN_PULP_TAG_KEY,
        'args': {
            'pulp_registry_name': 'test',
            'pulp_secret_path': str(tmpdir),
        }}])

    results = runner.run()
    assert msg in caplog.text()
    assert results['pulp_tag'] == expected_results
