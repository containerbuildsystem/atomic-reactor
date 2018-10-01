"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import os
import json

from flexmock import flexmock

from atomic_reactor.constants import (PLUGIN_FETCH_WORKER_METADATA_KEY,
                                      PLUGIN_COMPARE_COMPONENTS_KEY)
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor.util import ImageName

from tests.constants import MOCK_SOURCE, TEST_IMAGE, INPUT_IMAGE, FILES
from tests.docker_mock import mock_docker

import pytest


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
    setattr(workflow, 'postbuild_result', {})
    return workflow


def mock_metadatas():
    json_x_path = os.path.join(FILES, "example-koji-metadata-x86_64.json")
    json_p_path = os.path.join(FILES, "example-koji-metadata-ppc64le.json")

    with open(json_x_path) as json_data:
        metadatas_x = json.load(json_data)

    with open(json_p_path) as json_data:
        metadatas_p = json.load(json_data)

    # need to keep data separate otherwise deepcopy and edit 'arch'
    worker_metadatas = {
        'x86_64': metadatas_x,
        'ppc64le': metadatas_p,
    }

    return worker_metadatas


@pytest.mark.parametrize(('mismatch', 'exception', 'fail'), (
    (False, False, False),
    (True, False, True),
    (False, True, False),
    (True, True, False),
))
def test_compare_components_plugin(tmpdir, mismatch, exception, fail):
    workflow = mock_workflow(tmpdir)
    worker_metadatas = mock_metadatas()

    # example data has 2 log items before component item hence output[2]
    component = worker_metadatas['ppc64le']['output'][2]['components'][0]
    if mismatch:
        component['version'] = 'bacon'
    if exception:
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {
            WORKSPACE_CONF_KEY: ReactorConfig(
                {'version': 1, 'package_comparison_exceptions': [component['name']]}
            )
        }

    workflow.postbuild_results[PLUGIN_FETCH_WORKER_METADATA_KEY] = worker_metadatas

    runner = PostBuildPluginsRunner(
        None,
        workflow,
        [{
            'name': PLUGIN_COMPARE_COMPONENTS_KEY,
            "args": {}
        }]
    )

    if fail:
        with pytest.raises(PluginFailedException):
            runner.run()
    else:
        runner.run()


def test_no_components(tmpdir):
    workflow = mock_workflow(tmpdir)
    worker_metadatas = mock_metadatas()

    # example data has 2 log items before component item hence output[2]
    del worker_metadatas['x86_64']['output'][2]['components']
    del worker_metadatas['ppc64le']['output'][2]['components']

    workflow.postbuild_results[PLUGIN_FETCH_WORKER_METADATA_KEY] = worker_metadatas

    runner = PostBuildPluginsRunner(
        None,
        workflow,
        [{
            'name': PLUGIN_COMPARE_COMPONENTS_KEY,
            "args": {}
        }]
    )

    with pytest.raises(PluginFailedException):
        runner.run()


def test_bad_component_type(tmpdir):
    workflow = mock_workflow(tmpdir)
    worker_metadatas = mock_metadatas()

    # example data has 2 log items before component item hence output[2]
    worker_metadatas['x86_64']['output'][2]['components'][0]['type'] = "foo"

    workflow.postbuild_results[PLUGIN_FETCH_WORKER_METADATA_KEY] = worker_metadatas

    runner = PostBuildPluginsRunner(
        None,
        workflow,
        [{
            'name': PLUGIN_COMPARE_COMPONENTS_KEY,
            "args": {}
        }]
    )

    with pytest.raises(PluginFailedException):
        runner.run()
