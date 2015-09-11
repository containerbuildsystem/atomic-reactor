"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

try:
    import configparser
except ImportError:
    import ConfigParser as configparser
from contextlib import contextmanager

from flexmock import flexmock
import os
import pytest

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, AutoRebuildCanceledException
from atomic_reactor.plugins.pre_check_and_set_rebuild import CheckAndSetRebuildPlugin
from atomic_reactor.plugins.pre_stop_autorebuild_if_disabled import StopAutorebuildIfDisabledPlugin
from atomic_reactor.util import ImageName

from tests.constants import INPUT_IMAGE, MOCK, MOCK_SOURCE
if MOCK:
    from tests.docker_mock import mock_docker


@contextmanager
def mocked_configparser_getboolean(ret):
    # configparser.SafeConfigParser.getboolean can't be mocked in Py3.4 due to
    #  https://github.com/has207/flexmock/pull/100
    def getboolean(self, a, b):
        if isinstance(ret, bool):
            return ret
        else:
            raise ret
    old_gb = configparser.SafeConfigParser.getboolean
    configparser.SafeConfigParser.getboolean = getboolean
    yield
    configparser.SafeConfigParser.getboolean = old_gb


class Y(object):
    path = ''
    dockerfile_path = ''


class X(object):
    image_id = INPUT_IMAGE
    source = Y()
    base_image = ImageName.parse('asd')


class TestStopAutorebuildIfDisabledPlugin(object):
    prebuild_plugins = [{
        'name': StopAutorebuildIfDisabledPlugin.key,
        'args': {
            'config_file': '.osbs-repo-config'
        }
    }]

    def assert_message_logged(self, msg, cplog):
        assert any([msg in l.getMessage() for l in cplog.records()])

    def setup_method(self, method):
        if MOCK:
            mock_docker()
        self.tasker = DockerTasker()
        self.workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
        self.workflow.builder = X()

        def get():
            return 'path'

        self.workflow.source.get = get
        self.workflow.prebuild_results[CheckAndSetRebuildPlugin.key] = True
        self.runner = PreBuildPluginsRunner(self.tasker, self.workflow, self.prebuild_plugins)

    def test_disabled_in_config(self, caplog):
        if MOCK:
            mock_docker()

        flexmock(os.path).should_receive('exists').with_args('path/.osbs-repo-config').\
            and_return(True)
        flexmock(configparser.SafeConfigParser).should_receive('read').and_return(None)
        # flexmock(configparser.SafeConfigParser).should_receive('getboolean').\
        #     with_args('autorebuild', 'enabled').and_return(False)
        with mocked_configparser_getboolean(False):
            with pytest.raises(AutoRebuildCanceledException):
                self.runner.run()

        self.assert_message_logged('autorebuild is disabled in .osbs-repo-config', caplog)

    def test_enabled_in_config(self, caplog):
        if MOCK:
            mock_docker()

        flexmock(os.path).should_receive('exists').with_args('path/.osbs-repo-config').\
            and_return(True)
        flexmock(configparser.SafeConfigParser).should_receive('read').and_return(None)
        # flexmock(configparser.SafeConfigParser).should_receive('getboolean').\
        #     with_args('autorebuild', 'enabled').and_return(True)
        # assert this doesn't raise
        with mocked_configparser_getboolean(True):
            self.runner.run()
        self.assert_message_logged('autorebuild is enabled in .osbs-repo-config', caplog)

    def test_malformed_config(self, caplog):
        if MOCK:
            mock_docker()

        flexmock(os.path).should_receive('exists').with_args('path/.osbs-repo-config').\
            and_return(True)
        flexmock(configparser.SafeConfigParser).should_receive('read').and_return(None)
        # flexmock(configparser.SafeConfigParser).should_receive('getboolean').\
        #     with_args('autorebuild', 'enabled').and_raise(configparser.Error)
        # assert this doesn't raise
        with mocked_configparser_getboolean(configparser.Error):
            self.runner.run()
        self.assert_message_logged(
            'can\'t parse ".osbs-repo-config", assuming autorebuild is enabled',
            caplog)

    def test_no_config(self, caplog):
        if MOCK:
            mock_docker()

        # assert this doesn't raise
        self.runner.run()
        self.assert_message_logged('no ".osbs-repo-config", assuming autorebuild is enabled',
                                   caplog)
