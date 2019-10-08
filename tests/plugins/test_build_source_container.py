"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals, absolute_import

import os

from flexmock import flexmock
import pytest

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.core import DockerTasker
from atomic_reactor.plugin import BuildStepPluginsRunner
from atomic_reactor.plugins.build_source_container import SourceContainerPlugin
from atomic_reactor.plugins.pre_reactor_config import (
    WORKSPACE_CONF_KEY,
    ReactorConfig,
    ReactorConfigPlugin,
)
from tests.docker_mock import mock_docker
from tests.constants import TEST_IMAGE, MOCK_SOURCE


SOURCE_CONTAINERS_CONF = {'source_builder_image': "quay.io/ctrs/bsi:latest"}


class MockSource(object):

    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = os.path.join(tmpdir, 'Dockerfile')
        self.path = tmpdir
        self.config = flexmock(image_build_method=None)

    def get_build_file_path(self):
        return self.dockerfile_path, self.path


class MockInsideBuilder(object):

    def __init__(self):
        mock_docker()
        self.tasker = DockerTasker()
        self.base_image = None
        self.image_id = None
        self.image = None
        self.df_path = None
        self.df_dir = None
        self.parent_images_digests = {}

    def ensure_not_built(self):
        pass


def mock_workflow(tmpdir, source_containers_conf=None):
    workflow = DockerBuildWorkflow(MOCK_SOURCE, TEST_IMAGE)
    builder = MockInsideBuilder()
    source = MockSource(tmpdir)
    setattr(builder, 'source', source)
    setattr(workflow, 'source', source)
    setattr(workflow, 'builder', builder)

    workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
    if source_containers_conf is not None:
        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig({'version': 1, 'source_containers': source_containers_conf})

    return workflow


def test_running_build(tmpdir):
    """
    Test if proper result is returned and if plugin works
    """
    workflow = mock_workflow(
        tmpdir, source_containers_conf=SOURCE_CONTAINERS_CONF)
    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': SourceContainerPlugin.key,
            'args': {},
        }]
    )
    build_result = runner.run()
    assert not build_result.is_failed()
    assert build_result.oci_image_path


@pytest.mark.parametrize('source_containers_conf,emsg', [
    ({}, "Cannot build source containers, builder image is not specified in configuration"),
    (None, "Cannot build source containers, builder image is not specified in configuration"),
])
def test_incorrect_config(tmpdir, source_containers_conf, emsg):
    """
    Test if plugin reports proper errors for incorrect configuration
    """
    workflow = mock_workflow(
        tmpdir, source_containers_conf=source_containers_conf)
    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': SourceContainerPlugin.key,
            'args': {},
        }]
    )
    if emsg is not None:
        with pytest.raises(Exception) as exc_info:
            runner.run()
        assert emsg in str(exc_info.value)


def test_failed_build(tmpdir):
    """
    Test if proper error state is returned when build inside build
    container failed
    """
    workflow = mock_workflow(
        tmpdir, source_containers_conf=SOURCE_CONTAINERS_CONF)
    mocked_tasker = flexmock(workflow.builder.tasker)
    mocked_tasker.should_receive('wait').and_return(1)
    runner = BuildStepPluginsRunner(
        mocked_tasker,
        workflow,
        [{
            'name': SourceContainerPlugin.key,
            'args': {},
        }]
    )

    build_result = runner.run()
    assert build_result.is_failed()
