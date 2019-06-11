"""
Copyright (c) 2017, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import sys

from osbs.build.build_response import BuildResponse
from atomic_reactor.build import BuildResult
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugins.build_orchestrate_build import (OrchestrateBuildPlugin,
                                                            WORKSPACE_KEY_BUILD_INFO)
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor.util import ImageName
from atomic_reactor.plugins.exit_pulp_publish import PulpPublishPlugin
try:
    if sys.version_info.major > 2:
        # importing dockpulp in Python 3 causes SyntaxError
        raise ImportError

    import dockpulp
except (ImportError):
    dockpulp = None

import six
import pytest
from flexmock import flexmock
from tests.constants import SOURCE, MOCK
from tests.stubs import StubInsideBuilder, StubTagConf
if MOCK:
    from tests.docker_mock import mock_docker


class BuildInfo(object):
    def __init__(self, unset_annotations=False):
        annotations = {'meta': 'test'}
        if unset_annotations:
            annotations = None

        self.build = BuildResponse({'metadata': {'annotations': annotations}})


def prepare(success=True):
    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    workflow.builder = StubInsideBuilder()
    workflow.tag_conf = (StubTagConf()
                         .set_images([ImageName(repo="image-name1"),
                                      ImageName(repo="image-name1",
                                                tag="2"),
                                      ImageName(namespace="namespace",
                                                repo="image-name2"),
                                      ImageName(repo="image-name3",
                                                tag="asd")]))

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
     .with_args(six.text_type, six.text_type))
    (flexmock(dockpulp.Pulp)
     .should_receive('updateRepo')
     .with_args(six.text_type, dict))
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
            },
            'bogus': {},
        },
    }

    if success:
        workflow.build_result = BuildResult(image_id='12345')
    else:
        workflow.build_result = BuildResult(fail_reason="not built", annotations=annotations)

    build_info = {}
    build_info['x86_64'] = BuildInfo()
    build_info['ppc64le'] = BuildInfo()
    build_info['bogus'] = BuildInfo(unset_annotations=True)  # OSBS-5262

    workflow.plugin_workspace = {
        OrchestrateBuildPlugin.key: {
            WORKSPACE_KEY_BUILD_INFO: build_info
        }
    }

    mock_docker()
    return tasker, workflow


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
def test_pulp_publish_success(caplog, reactor_config_map):
    tasker, workflow = prepare(success=True)
    if reactor_config_map:
        pulp_map = {'name': 'pulp_registry_name', 'auth': {}}
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig({'version': 1, 'pulp': pulp_map})
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

    assert 'to be published' in caplog.text
    images = [i.to_str() for i in crane_images]
    assert "registry.example.com/image-name1:latest" in images
    assert "registry.example.com/image-name1:2" in images
    assert "registry.example.com/namespace/image-name2:latest" in images
    assert "registry.example.com/image-name3:asd" in images
