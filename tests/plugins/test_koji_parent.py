"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import os

try:
    import koji as koji
except ImportError:
    import inspect
    import sys

    # Find our mocked koji module
    import tests.koji as koji
    mock_koji_path = os.path.dirname(inspect.getfile(koji.ClientSession))
    if mock_koji_path not in sys.path:
        sys.path.append(os.path.dirname(mock_koji_path))

    # Now load it properly, the same way the plugin will
    del koji
    import koji as koji

from atomic_reactor.constants import INSPECT_CONFIG
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_koji_parent import KojiParentPlugin
from atomic_reactor.util import ImageName
from flexmock import flexmock
from tests.constants import MOCK, MOCK_SOURCE

import pytest

if MOCK:
    from tests.docker_mock import mock_docker


KOJI_HUB = 'http://koji.com/hub'

KOJI_BUILD_ID = 123456789

KOJI_BUILD_NVR = 'base-image-1.0-99'

KOJI_BUILD = {'nvr': KOJI_BUILD_NVR, 'id': KOJI_BUILD_ID}

BASE_IMAGE_LABELS = {
    'com.redhat.component': 'base-image',
    'version': '1.0',
    'release': '99',
}


class MockInsideBuilder(object):
    def __init__(self):
        self.tasker = DockerTasker()
        self.base_image = ImageName(repo='Fedora', tag='22')
        self.image_id = 'image_id'
        self.image = 'image'
        self.df_path = 'df_path'
        self.df_dir = 'df_dir'

    @property
    def source(self):
        result = flexmock()
        setattr(result, 'dockerfile_path', '/')
        setattr(result, 'path', '/tmp')
        return result


@pytest.fixture()
def workflow():
    if MOCK:
        mock_docker()
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.builder = MockInsideBuilder()

    flexmock(workflow, base_image_inspect={})
    workflow.base_image_inspect[INSPECT_CONFIG] = {'Labels': BASE_IMAGE_LABELS.copy()}

    return workflow


@pytest.fixture()
def koji_session():
    session = flexmock()
    flexmock(session).should_receive('getBuild').with_args(KOJI_BUILD_NVR).and_return(KOJI_BUILD)
    flexmock(koji).should_receive('ClientSession').and_return(session)
    return session


class TestKojiParent(object):

    def test_koji_build_found(self, workflow, koji_session):
        self.run_plugin_with_args(workflow)

    def test_koji_build_retry(self, workflow, koji_session):
        (flexmock(koji_session)
            .should_receive('getBuild')
            .with_args(KOJI_BUILD_NVR)
            .and_return(None)
            .and_return(None)
            .and_return(None)
            .and_return(None)
            .and_return(KOJI_BUILD)
            .times(5))

        self.run_plugin_with_args(workflow)

    def test_koji_ssl_certs_used(self, workflow, koji_session):
        certs_dir = '/my/super/secret/dir'
        expected_ssl_login_args = (
                '{}/cert'.format(certs_dir),
                '{}/ca'.format(certs_dir),
                '{}/serverca'.format(certs_dir),
                )
        (flexmock(koji_session)
            .should_receive('ssl_login')
            .with_args(*expected_ssl_login_args)
            .and_return(True))
        plugin_args = {'koji_ssl_certs_dir': certs_dir}
        self.run_plugin_with_args(workflow, plugin_args)

    def test_koji_build_not_found(self, workflow, koji_session):
        (flexmock(koji_session)
            .should_receive('getBuild')
            .with_args(KOJI_BUILD_NVR)
            .and_return(None))

        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow, {'poll_timeout': 0.01})
        assert 'build NOT found' in str(exc_info.value)

    def test_base_image_not_inspected(self, workflow, koji_session):
        del workflow.base_image_inspect[INSPECT_CONFIG]
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow)
        assert 'KeyError' in str(exc_info.value)
        assert 'Config' in str(exc_info.value)

    @pytest.mark.parametrize('remove_label', ('com.redhat.component', 'version', 'release'))
    def test_base_image_missing_labels(self, workflow, koji_session, remove_label):
        del workflow.base_image_inspect[INSPECT_CONFIG]['Labels'][remove_label]
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow)
        assert 'KeyError' in str(exc_info.value)
        assert remove_label in str(exc_info.value)

    def run_plugin_with_args(self, workflow, plugin_args=None):
        plugin_args = plugin_args or {}
        plugin_args.setdefault('koji_hub', KOJI_HUB)
        plugin_args.setdefault('poll_interval', 0.01)
        plugin_args.setdefault('poll_timeout', 1)

        runner = PreBuildPluginsRunner(
            workflow.builder.tasker,
            workflow,
            [{'name': KojiParentPlugin.key, 'args': plugin_args}]
        )

        result = runner.run()
        assert result[KojiParentPlugin.key] == {'parent-image-koji-build-id': KOJI_BUILD_ID}
