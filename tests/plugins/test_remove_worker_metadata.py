"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import os

from flexmock import flexmock

from atomic_reactor.core import DockerTasker
from atomic_reactor.constants import PLUGIN_REMOVE_WORKER_METADATA_KEY
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import ExitPluginsRunner
from atomic_reactor.util import ImageName
from atomic_reactor.plugins.exit_remove_worker_metadata import (defer_removal)
from osbs.exceptions import OsbsResponseException

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

    def delete_config_map(self, name):
        if name:
            return name in self.config_map
        else:
            raise OsbsResponseException("Failed to delete config map", 404)


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


@pytest.mark.parametrize('names', [['build-1-x86_64-md'],
                                   ['build-1-ppc64le-md'],
                                   ['build-1-x86_64-md', 'build-1-ppc64le-md'],
                                   [None]])
@pytest.mark.parametrize('fragment_key', ['metadata.json', None])
def test_remove_worker_plugin(tmpdir, caplog, names, fragment_key):
    workflow = mock_workflow(tmpdir)

    koji_metadata = {
        'foo': 'bar',
        'spam': 'bacon',
    }
    metadata = {'metadata.json': koji_metadata}

    for name in names:
        osbs = MockOSBS({name: metadata})
        defer_removal(workflow, name, osbs)

        (flexmock(osbs)
         .should_call("delete_config_map")
         .with_args(name)
         .once()
         .and_return(True))

    runner = ExitPluginsRunner(
        None,
        workflow,
        [{
            'name': PLUGIN_REMOVE_WORKER_METADATA_KEY,
            "args": {}
        }]
    )

    runner.run()

    for name in names:
        if name:
            assert "ConfigMap {} deleted".format(name) in caplog.text
        else:
            assert "Failed to delete ConfigMap None" in caplog.text
