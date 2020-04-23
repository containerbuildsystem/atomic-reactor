"""
Copyright (c) 2017, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import koji

from atomic_reactor.core import DockerTasker
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_inject_parent_image import InjectParentImage
from atomic_reactor.plugins.exit_remove_built_image import GarbageCollectionPlugin
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor.util import ImageName
from flexmock import flexmock
from tests.constants import MOCK
from osbs.utils import graceful_chain_del

import copy
import pytest

if MOCK:
    from tests.docker_mock import mock_docker


KOJI_HUB = 'http://koji.com/hub'

KOJI_BUILD_ID = 123456789

KOJI_BUILD_NVR = 'base-image-1.0-99'

KOJI_BUILD_INFO = {'nvr': KOJI_BUILD_NVR, 'id': KOJI_BUILD_ID}

ARCHIVES = [
    {'id': 1},
    {'id': 2, 'extra': {}},
    {'id': 3, 'extra': {
        'docker': {
            'repositories': [
                'spam.com/fedora:27-3',
                'spam.com/fedora@sha256:'
                '07cc0fb792aad1b1891354d6a21086038d486e5a05eb76dbe4f8648f0767c53e'
            ],
        }
    }},
]

USE_DEFAULT_ARCHIVES = object()
USE_DEFAULT_KOJI_BUILD_INFO = object()


class MockInsideBuilder(object):
    def __init__(self):
        self.tasker = DockerTasker()
        self.base_image = ImageName(repo='fedora', tag='26')
        self.original_base_image = ImageName(repo='fedora', tag='26')
        self.base_from_scratch = False
        self.custom_base_image = False
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

    def set_base_image(self, base_image):
        self.base_image = ImageName.parse(base_image)


@pytest.fixture()
def workflow(workflow):
    if MOCK:
        mock_docker()
    workflow.builder = MockInsideBuilder()
    setattr(workflow.builder, 'base_image_inspect', {})

    return workflow


def koji_session(koji_build_id=KOJI_BUILD_ID, koji_build_info=USE_DEFAULT_KOJI_BUILD_INFO,
                 archives=USE_DEFAULT_ARCHIVES):
    if archives == USE_DEFAULT_ARCHIVES:
        archives = copy.deepcopy(ARCHIVES)
    if koji_build_info == USE_DEFAULT_KOJI_BUILD_INFO:
        koji_build_info = copy.deepcopy(KOJI_BUILD_INFO)
    session = flexmock()

    def mock_get_build(requested_build_id):
        if str(requested_build_id) == str(koji_build_id):
            return koji_build_info
        return None

    flexmock(session).should_receive('getBuild').replace_with(mock_get_build)
    # Aways expect build ID to be used, even when NVR is given.
    flexmock(session).should_receive('listArchives').with_args(KOJI_BUILD_ID).and_return(archives)
    flexmock(koji).should_receive('ClientSession').and_return(session)
    flexmock(session).should_receive('krb_login').and_return(True)
    return session


class TestKojiParent(object):

    @pytest.mark.parametrize('base_from_scratch', [True, False])  # noqa
    @pytest.mark.parametrize('custom_base_image', [True, False])
    def test_parent_image_injected(self, caplog, workflow, reactor_config_map,
                                   base_from_scratch, custom_base_image):
        koji_session()
        previous_parent_image = workflow.builder.base_image
        workflow.builder.base_from_scratch = base_from_scratch
        workflow.builder.custom_base_image = custom_base_image
        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map,
                                  base_from_scratch=base_from_scratch,
                                  custom_base_image=custom_base_image)
        if base_from_scratch:
            assert str(previous_parent_image) == str(workflow.builder.base_image)

            log_msg = "from scratch can't inject parent image"
            assert log_msg in caplog.text
        elif custom_base_image:
            assert str(previous_parent_image) == str(workflow.builder.base_image)

            log_msg = "custom base image builds can't inject parent image"
            assert log_msg in caplog.text
        else:
            assert str(previous_parent_image) != str(workflow.builder.base_image)

    @pytest.mark.parametrize('koji_build', (KOJI_BUILD_ID, KOJI_BUILD_NVR, str(KOJI_BUILD_ID)))
    def test_koji_build_identifier(self, workflow, koji_build, reactor_config_map):
        koji_session(koji_build_id=koji_build)
        self.run_plugin_with_args(workflow, plugin_args={'koji_parent_build': koji_build},
                                  reactor_config_map=reactor_config_map)

    def test_unknown_koji_build(self, workflow, reactor_config_map):  # noqa
        koji_session()
        unknown_build = KOJI_BUILD_ID + 1
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow, plugin_args={'koji_parent_build': unknown_build},
                                      reactor_config_map=reactor_config_map)
        assert '{}, not found'.format(unknown_build) in str(exc_info.value)

    @pytest.mark.parametrize(('repositories', 'selected'), (
        ([':26-3', '@sha256:12345'], '@sha256:12345'),
        ([':26-3', ':26-spam'], ':26-3'),
    ))
    def test_repository_from_koji_build(self, workflow, repositories, selected,
                                        reactor_config_map):
        # Populate archives to ensure koji build takes precedence
        archives = [
            {'id': 1, 'extra': {'docker': {'repositories': [
                'spam.com/notselected/fedora{}'.format(repo) for repo in repositories
            ]}}}
        ]

        repo_template = 'spam.com/fedora{}'
        koji_build_info = copy.deepcopy(KOJI_BUILD_INFO)
        koji_build_info['extra'] = {'image': {'index': {'pull': [
            repo_template.format(repo) for repo in repositories
        ]}}}

        koji_session(archives=archives, koji_build_info=koji_build_info)
        workflow.builder.base_image = ImageName.parse('spam.com/fedora:some_tag')
        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)
        assert str(workflow.builder.base_image) == repo_template.format(selected)

    @pytest.mark.parametrize('organization', [None, 'my_organization'])  # noqa
    @pytest.mark.parametrize('archive_registry', ['spam.com', 'old_registry.com'])
    @pytest.mark.parametrize(('repositories', 'selected'), (
        ([':26-3', '@sha256:12345'], '@sha256:12345'),
        ([':26-3', ':26-spam'], ':26-3'),
    ))
    def test_repository_selection(self, workflow, organization, archive_registry,
                                  repositories, selected, reactor_config_map):
        archive_repo_template = archive_registry + '/fedora{}'
        archives = [
            {'id': 1, 'extra': {'docker': {'repositories': [
                archive_repo_template.format(repo) for repo in repositories
            ]}}}
        ]
        enclosed_repo_template = 'spam.com/{}/fedora{}'
        repo_template = 'spam.com/fedora{}'

        koji_session(archives=archives)
        workflow.builder.base_image = ImageName.parse('spam.com/fedora:some_tag')
        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map,
                                  organization=organization)
        if organization and reactor_config_map:
            selected_repo = enclosed_repo_template.format(organization, selected)
        else:
            selected_repo = repo_template.format(selected)

        assert str(workflow.builder.base_image) == selected_repo

    @pytest.mark.parametrize(('repository', 'is_valid'), (
        ('fedora', True),
        ('rawhide/fedora', False),
        ('centos', False),
    ))
    def test_new_parent_image_validation(self, workflow, repository, is_valid,
                                         reactor_config_map):
        archives = [
            {'id': 1, 'extra': {'docker': {'repositories': [
                'spam.com/{}@sha256:12345'.format(repository),
            ]}}}
        ]

        koji_session(archives=archives)
        if is_valid:
            self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)
        else:
            with pytest.raises(PluginFailedException) as exc_info:
                self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)
            assert 'differs from repository for existing parent image' in str(exc_info.value)

    def test_koji_ssl_certs_used(self, tmpdir, workflow, reactor_config_map):  # noqa
        session = koji_session()
        serverca = tmpdir.join('serverca')
        serverca.write('spam')
        expected_ssl_login_args = {
            'cert': str(tmpdir.join('cert')),
            'serverca': str(serverca),
            'ca': None,
        }
        (flexmock(session)
            .should_receive('ssl_login')
            .with_args(**expected_ssl_login_args)
            .and_return(True)
            .once())
        plugin_args = {'koji_ssl_certs_dir': str(tmpdir)}
        self.run_plugin_with_args(workflow, plugin_args, reactor_config_map=reactor_config_map)

    def test_no_archives(self, workflow, reactor_config_map):  # noqa
        koji_session(archives=[])
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)
        assert 'A suitable archive' in str(exc_info.value)
        assert 'not found' in str(exc_info.value)

    def test_no_repositories(self, workflow, reactor_config_map):  # noqa
        archives = copy.deepcopy(ARCHIVES)
        for archive in archives:
            graceful_chain_del(archive, 'extra', 'docker', 'repositories')

        koji_session(archives=archives)
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)
        assert 'A suitable archive' in str(exc_info.value)
        assert 'not found' in str(exc_info.value)

    def test_skip_build(self, workflow, caplog, reactor_config_map):  # noqa
        koji_session(archives=[])
        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map,
                                  koji_parent_build='')

        assert 'no koji parent build, skipping plugin' in caplog.text

    def run_plugin_with_args(self, workflow, plugin_args=None, reactor_config_map=False,  # noqa
                             organization=None, base_from_scratch=False, custom_base_image=False,
                             koji_parent_build=KOJI_BUILD_ID):
        plugin_args = plugin_args or {}
        plugin_args.setdefault('koji_parent_build', koji_parent_build)
        plugin_args.setdefault('koji_hub', KOJI_HUB)

        if reactor_config_map:
            koji_map = {
                'hub_url': KOJI_HUB,
                'root_url': '',
                'auth': {}}
            if 'koji_ssl_certs_dir' in plugin_args:
                koji_map['auth']['ssl_certs_dir'] = plugin_args['koji_ssl_certs_dir']
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1, 'koji': koji_map,
                               'registries_organization': organization})

        runner = PreBuildPluginsRunner(
            workflow.builder.tasker,
            workflow,
            [{'name': InjectParentImage.key, 'args': plugin_args}]
        )

        result = runner.run()
        if not koji_parent_build:
            return
        if base_from_scratch or custom_base_image:
            assert result[InjectParentImage.key] is None
        else:
            # Koji build ID is always used, even when NVR is given.
            assert result[InjectParentImage.key] == KOJI_BUILD_ID
            self.assert_images_to_remove(workflow)

    def assert_images_to_remove(self, workflow):
        expected = set([str(workflow.builder.base_image)])
        actual = workflow.plugin_workspace[GarbageCollectionPlugin.key]['images_to_remove']
        assert actual == expected
