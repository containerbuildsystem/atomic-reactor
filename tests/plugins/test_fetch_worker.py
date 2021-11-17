"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import logging

from atomic_reactor.constants import PLUGIN_FETCH_WORKER_METADATA_KEY
from atomic_reactor.inner import BuildResult
from atomic_reactor.plugin import PostBuildPluginsRunner
from atomic_reactor.plugins.build_orchestrate_build import (WorkerBuildInfo, ClusterInfo,
                                                            OrchestrateBuildPlugin)

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


@pytest.mark.parametrize('fragment_key', ['metadata.json', None])
def test_fetch_worker_plugin(fragment_key, workflow):
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
