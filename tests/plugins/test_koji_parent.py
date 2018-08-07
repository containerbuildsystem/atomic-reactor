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

from atomic_reactor.constants import (
    INSPECT_CONFIG, BASE_IMAGE_KOJI_BUILD, PARENT_IMAGES_KOJI_BUILDS
)
from atomic_reactor.build import InsideBuilder
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_koji_parent import KojiParentPlugin
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor.util import ImageName
from flexmock import flexmock
from tests.fixtures import reactor_config_map  # noqa
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

BASE_IMAGE_LABELS_W_ALIASES = {
    'com.redhat.component': 'base-image',
    'BZComponent': 'base-image',
    'version': '1.0',
    'Version': '1.0',
    'release': '99',
    'Release': '99',
}


class MockInsideBuilder(InsideBuilder):
    def __init__(self):
        self.tasker = flexmock()
        self.base_image = ImageName(repo='Fedora', tag='22')
        self.original_base_image = ImageName(repo='Fedora', tag='22')
        self.parent_images = {}  # don't want to handle inspections in most tests
        self._parent_images_inspect = {}
        self.image_id = 'image_id'
        self.image = 'image'
        self._df_path = 'df_path'
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
    base_inspect = {INSPECT_CONFIG: {'Labels': BASE_IMAGE_LABELS.copy()}}
    flexmock(workflow.builder, base_image_inspect=base_inspect)

    return workflow


@pytest.fixture()
def koji_session():
    session = flexmock()
    flexmock(session).should_receive('getBuild').with_args(KOJI_BUILD_NVR).and_return(KOJI_BUILD)
    flexmock(session).should_receive('krb_login').and_return(True)
    flexmock(koji).should_receive('ClientSession').and_return(session)
    return session


class TestKojiParent(object):

    def test_koji_build_found(self, workflow, koji_session, reactor_config_map):  # noqa
        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)

    def test_koji_build_retry(self, workflow, koji_session, reactor_config_map):  # noqa
        (flexmock(koji_session)
            .should_receive('getBuild')
            .with_args(KOJI_BUILD_NVR)
            .and_return(None)
            .and_return(None)
            .and_return(None)
            .and_return(None)
            .and_return(KOJI_BUILD)
            .times(5))

        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)

    def test_koji_ssl_certs_used(self, tmpdir, workflow, koji_session, reactor_config_map):  # noqa
        serverca = tmpdir.join('serverca')
        serverca.write('spam')
        expected_ssl_login_args = {
            'cert': str(tmpdir.join('cert')),
            'serverca': str(serverca),
            'ca': None,
        }
        (flexmock(koji_session)
            .should_receive('ssl_login')
            .with_args(**expected_ssl_login_args)
            .and_return(True)
            .once())
        plugin_args = {'koji_ssl_certs_dir': str(tmpdir)}
        self.run_plugin_with_args(workflow, plugin_args, reactor_config_map=reactor_config_map)

    def test_koji_build_not_found(self, workflow, koji_session, reactor_config_map):  # noqa
        (flexmock(koji_session)
            .should_receive('getBuild')
            .with_args(KOJI_BUILD_NVR)
            .and_return(None))

        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow, {'poll_timeout': 0.01},
                                      reactor_config_map=reactor_config_map)
        assert 'KojiParentBuildMissing' in str(exc_info.value)

    def test_base_image_not_inspected(self, workflow, koji_session, reactor_config_map):  # noqa
        del workflow.builder.base_image_inspect[INSPECT_CONFIG]
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)
        assert 'KeyError' in str(exc_info.value)
        assert 'Config' in str(exc_info.value)

    @pytest.mark.parametrize(('remove_labels', 'exp_result'), [  # noqa: F811
        (['com.redhat.component'], True),
        (['BZComponent'], True),
        (['com.redhat.component', 'BZComponent'], False),
        (['version'], True),
        (['Version'], True),
        (['version', 'Version'], False),
        (['release'], True),
        (['Release'], True),
        (['release', 'Release'], False),
    ])
    def test_base_image_missing_labels(self, workflow, koji_session, remove_labels, exp_result,
                                       reactor_config_map):
        workflow.builder.base_image_inspect[INSPECT_CONFIG]['Labels'] =\
            BASE_IMAGE_LABELS_W_ALIASES.copy()
        for label in remove_labels:
            del workflow.builder.base_image_inspect[INSPECT_CONFIG]['Labels'][label]
        self.run_plugin_with_args(workflow, expect_result=exp_result,
                                  reactor_config_map=reactor_config_map)

    def test_multiple_parent_images(self, workflow, koji_session, reactor_config_map):  # noqa: F811
        parent_images = dict(
            somebuilder='b1tag',
            otherbuilder='b2tag',
            base='basetag',
            unresolved=None,
        )
        koji_builds = dict(
            somebuilder=dict(nvr='somebuilder-1.0-1', id=42),
            otherbuilder=dict(nvr='otherbuilder-2.0-1', id=43),
            base=dict(nvr='base-16.0-1', id=16),
            unresolved=None,
        )
        image_inspects = {}

        # need to load up our mock objects with expected responses for the parents
        for img, build in koji_builds.items():
            if build is None:
                continue
            name, version, release = koji_builds[img]['nvr'].split('-')
            labels = {'com.redhat.component': name, 'version': version, 'release': release}
            image_inspects[img] = {INSPECT_CONFIG: dict(Labels=labels)}
            (workflow.builder.tasker
                .should_receive('inspect_image')
                .with_args(parent_images[img])
                .and_return(image_inspects[img]))
            (koji_session.should_receive('getBuild')
                .with_args(koji_builds[img]['nvr'])
                .and_return(koji_builds[img]))

        workflow.builder.set_base_image('basetag')
        workflow.builder.parent_images = parent_images
        workflow.builder.base_image_inspect.update(image_inspects['base'])

        expected = {
            BASE_IMAGE_KOJI_BUILD: koji_builds['base'],
            PARENT_IMAGES_KOJI_BUILDS: koji_builds,
        }
        self.run_plugin_with_args(
            workflow, expect_result=expected, reactor_config_map=reactor_config_map
        )

    def run_plugin_with_args(self, workflow, plugin_args=None, expect_result=True,  # noqa
                             reactor_config_map=False):
        plugin_args = plugin_args or {}
        plugin_args.setdefault('koji_hub', KOJI_HUB)
        plugin_args.setdefault('poll_interval', 0.01)
        plugin_args.setdefault('poll_timeout', 1)

        if reactor_config_map:

            koji_map = {
                'hub_url': KOJI_HUB,
                'root_url': '',
                'auth': {}
            }
            if 'koji_ssl_certs_dir' in plugin_args:
                koji_map['auth']['ssl_certs_dir'] = plugin_args['koji_ssl_certs_dir']
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1, 'koji': koji_map})

        runner = PreBuildPluginsRunner(
            workflow.builder.tasker,
            workflow,
            [{'name': KojiParentPlugin.key, 'args': plugin_args}]
        )

        result = runner.run()
        if expect_result is True:
            expected_result = {BASE_IMAGE_KOJI_BUILD: KOJI_BUILD}
        elif expect_result is False:
            expected_result = None
        else:  # param provided the expected result
            expected_result = expect_result

        assert result[KojiParentPlugin.key] == expected_result
