"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals, absolute_import

import os
import subprocess

from flexmock import flexmock

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.core import DockerTasker
from atomic_reactor.plugin import BuildStepPluginsRunner
from atomic_reactor.plugins.build_source_container import SourceContainerPlugin
from atomic_reactor.plugins.pre_reactor_config import (
    ReactorConfigPlugin,
)
from tests.docker_mock import mock_docker
from tests.constants import TEST_IMAGE, MOCK_SOURCE


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


def mock_workflow(tmpdir):
    workflow = DockerBuildWorkflow(TEST_IMAGE, source=MOCK_SOURCE)
    builder = MockInsideBuilder()
    source = MockSource(tmpdir)
    setattr(builder, 'source', source)
    setattr(workflow, 'source', source)
    setattr(workflow, 'builder', builder)

    workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

    return workflow


def test_running_build(tmpdir, caplog):
    """
    Test if proper result is returned and if plugin works
    """
    flexmock(subprocess).should_receive('check_output').and_return('stub stdout')
    workflow = mock_workflow(tmpdir)
    mocked_tasker = flexmock(workflow.builder.tasker)
    mocked_tasker.should_receive('wait').and_return(0)
    runner = BuildStepPluginsRunner(
        mocked_tasker,
        workflow,
        [{
            'name': SourceContainerPlugin.key,
            'args': {},
        }]
    )
    build_result = runner.run()
    assert not build_result.is_failed()
    assert build_result.oci_image_path
    assert 'stub stdout' in caplog.text


def test_failed_build(tmpdir, caplog):
    """
    Test if proper error state is returned when build inside build
    container failed
    """
    (flexmock(subprocess).should_receive('check_output')
     .and_raise(subprocess.CalledProcessError(1, 'cmd', output='stub stdout')))
    workflow = mock_workflow(tmpdir)
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
    assert 'BSI failed with output:' in caplog.text
    assert 'stub stdout' in caplog.text
