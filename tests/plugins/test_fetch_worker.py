"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import os
import logging

from flexmock import flexmock

from atomic_reactor.core import DockerTasker
from atomic_reactor.constants import PLUGIN_FETCH_WORKER_METADATA_KEY
from atomic_reactor.build import BuildResult
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner
from atomic_reactor.util import ImageName

from atomic_reactor.plugins.build_orchestrate_build import (WorkerBuildInfo, ClusterInfo,
                                                            OrchestrateBuildPlugin)

from tests.constants import MOCK_SOURCE, TEST_IMAGE, INPUT_IMAGE
from tests.docker_mock import mock_docker
import pytest


class MockConfigMapResponse(object):
    def __init__(self, data):
        self.data = data

    def get_data_by_key(self, key):
        return self.data[key]


class MockOSBS(object):
    def __init__(self, config_map):
        self.config_map = config_map

    def get_config_map(self, name):
        return MockConfigMapResponse(self.config_map[name])


class MockSource(object):

    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = os.path.join(tmpdir, 'Dockerfile')
        self.path = tmpdir

    def get_dockerfile_path(self):
        return self.dockerfile_path, self.path


class MockInsideBuilder(object):

    def __init__(self):
        mock_docker()
        self.tasker = DockerTasker()
        self.base_image = ImageName(repo='fedora', tag='25')
        self.image_id = 'image_id'
        self.image = INPUT_IMAGE
        self.df_path = 'df_path'
        self.df_dir = 'df_dir'

        def simplegen(x, y):
            yield "some\u2018".encode('utf-8')
        flexmock(self.tasker, build_image_from_path=simplegen)

    def get_built_image_info(self):
        return {'Id': 'some'}

    def inspect_built_image(self):
        return None

    def ensure_not_built(self):
        pass


def mock_workflow(tmpdir):
    workflow = DockerBuildWorkflow(MOCK_SOURCE, TEST_IMAGE)
    setattr(workflow, 'builder', MockInsideBuilder())
    setattr(workflow, 'source', MockSource(tmpdir))
    setattr(workflow.builder, 'source', MockSource(tmpdir))

    return workflow


@pytest.mark.parametrize('fragment_key', ['metadata.json', None])
def test_fetch_worker_plugin(tmpdir, fragment_key):
    workflow = mock_workflow(tmpdir)

    annotations = {
        'worker-builds': {
            'x86_64': {
                'build': {
                    'build-name': 'build-1-x64_64',
                },
                'metadata_fragment': 'configmap/build-1-x86_64-md',
                'metadata_fragment_key': fragment_key,
            },
            'ppc64le': {
                'build': {
                    'build-name': 'build-1-ppc64le',
                },
                'metadata_fragment': 'configmap/build-1-ppc64le-md',
                'metadata_fragment_key': fragment_key,
            }
        }
    }
    koji_metadata = {
        'foo': 'bar',
        'spam': 'bacon',
    }
    metadata = {'metadata.json': koji_metadata}
    log = logging.getLogger("atomic_reactor.plugins." + OrchestrateBuildPlugin.key)

    build = None
    cluster = None
    load = None

    name = 'build-1-x86_64-md'
    osbs = MockOSBS({name: metadata})
    cluster_info = ClusterInfo(cluster, 'x86_64', osbs, load)
    worker_x86_64 = WorkerBuildInfo(build, cluster_info, log)

    name = 'build-1-ppc64le-md'
    osbs = MockOSBS({name: metadata})
    cluster_info = ClusterInfo(cluster, "ppc64le", osbs, load)
    worker_ppc64le = WorkerBuildInfo(build, cluster_info, log)

    workspace = {
        'build_info': {
            'x86_64': worker_x86_64,
            'ppc64le': worker_ppc64le,
        },
        'koji_upload_dir': 'foo',
    }

    workflow.build_result = BuildResult(annotations=annotations, image_id="id1234")
    workflow.plugin_workspace[OrchestrateBuildPlugin.key] = workspace

    runner = PostBuildPluginsRunner(
        None,
        workflow,
        [{
            'name': PLUGIN_FETCH_WORKER_METADATA_KEY,
            "args": {}
        }]
    )

    output = runner.run()
    expected = {
        'fetch_worker_metadata': {
            'x86_64': koji_metadata,
            'ppc64le': koji_metadata,
        }
    }
    expected_failed = {'fetch_worker_metadata': {}}

    if fragment_key:
        assert output == expected
    else:
        assert output == expected_failed
