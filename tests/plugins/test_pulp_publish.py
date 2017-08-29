"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import sys

from osbs.build.build_response import BuildResponse
from atomic_reactor.build import BuildResult
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugins.build_orchestrate_build import (OrchestrateBuildPlugin,
                                                            WORKSPACE_KEY_BUILD_INFO)
from atomic_reactor.util import ImageName
from atomic_reactor.plugins.exit_pulp_publish import PulpPublishPlugin
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


def prepare(success=True, v1_image_ids={}):
    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)
    setattr(workflow, 'tag_conf', X())
    setattr(workflow.tag_conf, 'images', [ImageName(repo="image-name1"),
                                          ImageName(repo="image-name1",
                                                    tag="2"),
                                          ImageName(namespace="namespace",
                                                    repo="image-name2"),
                                          ImageName(repo="image-name3",
                                                    tag="asd")])

    # Mock dockpulp and docker
    dockpulp.Pulp = flexmock(dockpulp.Pulp)
    dockpulp.Pulp.registry = 'registry.example.com'
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
         {"id": "redhat-namespace-image-name2"}
      ]))
    (flexmock(dockpulp.Pulp)
     .should_receive('createRepo'))
    (flexmock(dockpulp.Pulp)
     .should_receive('copy')
     .with_args(unicode, unicode))
    (flexmock(dockpulp.Pulp)
     .should_receive('updateRepo')
     .with_args(unicode, dict))
    (flexmock(dockpulp.Pulp)
     .should_receive('')
     .with_args(object, object)
     .and_return([1, 2, 3]))

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

    if success:
        workflow.build_result = BuildResult(image_id='12345')
    else:
        workflow.build_result = BuildResult(fail_reason="not built", annotations=annotations)

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
def test_pulp_publish_success(caplog):
    tasker, workflow = prepare(success=True)
    plugin = PulpPublishPlugin(tasker, workflow, 'pulp_registry_name')

    (flexmock(dockpulp.Pulp).should_receive('crane')
     .with_args(set(['redhat-image-name1',
                     'redhat-image-name3',
                     'redhat-namespace-image-name2']),
                wait=True)
     .and_return([]))
    (flexmock(dockpulp.Pulp)
     .should_receive('watch_tasks')
     .with_args(list))

    crane_images = plugin.run()

    assert 'to be published' in caplog.text()
    images = [i.to_str() for i in crane_images]
    assert "registry.example.com/image-name1:latest" in images
    assert "registry.example.com/image-name1:2" in images
    assert "registry.example.com/namespace/image-name2:latest" in images
    assert "registry.example.com/image-name3:asd" in images


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
@pytest.mark.parametrize(('worker_builds_created'), [True, False])
@pytest.mark.parametrize(("v1_image_ids", "expected"), [
    ({'x86_64': None, 'ppc64le': None}, False),
    ({'x86_64': None, 'ppc64le': 'ppc64le_v1_image_id'}, True),
])
def test_pulp_publish_delete(worker_builds_created, v1_image_ids,
                             expected, caplog):
    tasker, workflow = prepare(success=False, v1_image_ids=v1_image_ids)
    if not worker_builds_created:
        workflow.build_result = BuildResult(fail_reason="not built")

    plugin = PulpPublishPlugin(tasker, workflow, 'pulp_registry_name')
    msg = "removing ppc64le_v1_image_id from"

    (flexmock(dockpulp.Pulp).should_receive('crane').never())
    if expected:
        (flexmock(dockpulp.Pulp).should_receive('remove').with_args(unicode, unicode))
    else:
        (flexmock(dockpulp.Pulp).should_receive('remove').never())

    crane_images = plugin.run()

    assert crane_images == []
    if expected and worker_builds_created:
        assert msg in caplog.text()
    else:
        assert msg not in caplog.text()
