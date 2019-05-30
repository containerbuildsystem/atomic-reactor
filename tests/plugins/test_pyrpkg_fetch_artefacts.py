"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import pytest
import os

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins import pre_pyrpkg_fetch_artefacts
from atomic_reactor.plugins.pre_pyrpkg_fetch_artefacts import DistgitFetchArtefactsPlugin
from atomic_reactor.util import ImageName
from flexmock import flexmock
from tests.constants import INPUT_IMAGE, MOCK_SOURCE


class Y(object):
    dockerfile_path = None
    path = None


class X(object):
    image_id = INPUT_IMAGE
    source = Y()
    base_image = ImageName.parse('asd')


def test_distgit_fetch_artefacts_plugin(tmpdir, docker_tasker):  # noqa
    command = 'fedpkg sources'
    expected_command = ['fedpkg', 'sources']

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.builder = X()
    workflow.source = flexmock(path=str(tmpdir))

    initial_dir = os.getcwd()
    assert initial_dir != str(tmpdir)

    def assert_tmpdir(*args, **kwargs):
        assert os.getcwd() == str(tmpdir)

    (flexmock(pre_pyrpkg_fetch_artefacts.subprocess)
        .should_receive('check_call')
        .with_args(expected_command)
        .replace_with(assert_tmpdir)
        .once())

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': DistgitFetchArtefactsPlugin.key,
            'args': {'command': command}
        }]
    )
    runner.run()

    assert os.getcwd() == initial_dir


def test_distgit_fetch_artefacts_failure(tmpdir, docker_tasker):  # noqa
    command = 'fedpkg sources'
    expected_command = ['fedpkg', 'sources']

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.builder = X()
    workflow.source = flexmock(path=str(tmpdir))

    initial_dir = os.getcwd()
    assert initial_dir != str(tmpdir)

    (flexmock(pre_pyrpkg_fetch_artefacts.subprocess)
        .should_receive('check_call')
        .with_args(expected_command)
        .and_raise(RuntimeError)
        .once())

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': DistgitFetchArtefactsPlugin.key,
            'args': {'command': command}
        }]
    )
    with pytest.raises(PluginFailedException):
        runner.run()

    assert os.getcwd() == initial_dir
