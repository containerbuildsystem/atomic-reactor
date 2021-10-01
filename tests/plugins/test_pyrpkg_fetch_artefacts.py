"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import pytest
import os

from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins import pre_pyrpkg_fetch_artefacts
from atomic_reactor.plugins.pre_pyrpkg_fetch_artefacts import DistgitFetchArtefactsPlugin
from osbs.utils import ImageName
from tests.stubs import StubSource
from flexmock import flexmock
from tests.constants import INPUT_IMAGE


class Y(object):
    dockerfile_path = None
    path = None


class X(object):
    image_id = INPUT_IMAGE
    source = Y()
    base_image = ImageName.parse('asd')


def test_distgit_fetch_artefacts_plugin(tmpdir, workflow):  # noqa
    command = 'fedpkg sources'
    expected_command = ['fedpkg', 'sources']

    workflow.source = StubSource()
    workflow.source.path = str(tmpdir)

    initial_dir = os.getcwd()
    assert initial_dir != str(tmpdir)

    def assert_tmpdir(*args, **kwargs):
        assert os.getcwd() == str(tmpdir)

    (flexmock(pre_pyrpkg_fetch_artefacts.subprocess)
        .should_receive('check_call')
        .with_args(expected_command)
        .replace_with(assert_tmpdir)
        .once())
    workflow.conf.conf['sources_command'] = command

    runner = PreBuildPluginsRunner(
        workflow,
        [{
            'name': DistgitFetchArtefactsPlugin.key,
        }]
    )
    runner.run()

    assert os.getcwd() == initial_dir


def test_distgit_fetch_artefacts_failure(tmpdir, workflow):  # noqa
    command = 'fedpkg sources'
    expected_command = ['fedpkg', 'sources']

    workflow.source = StubSource()
    workflow.source.path = str(tmpdir)

    initial_dir = os.getcwd()
    assert initial_dir != str(tmpdir)

    (flexmock(pre_pyrpkg_fetch_artefacts.subprocess)
        .should_receive('check_call')
        .with_args(expected_command)
        .and_raise(RuntimeError)
        .once())
    workflow.conf.conf['sources_command'] = command

    runner = PreBuildPluginsRunner(
        workflow,
        [{
            'name': DistgitFetchArtefactsPlugin.key,
        }]
    )
    with pytest.raises(PluginFailedException):
        runner.run()

    assert os.getcwd() == initial_dir


def test_distgit_fetch_artefacts_skip(tmpdir, workflow, caplog):  # noqa
    workflow.source = StubSource()
    workflow.source.path = str(tmpdir)

    runner = PreBuildPluginsRunner(
        workflow,
        [{
            'name': DistgitFetchArtefactsPlugin.key,
        }]
    )
    runner.run()

    log_msg = 'no sources command configuration, skipping plugin'
    assert log_msg in caplog.text
