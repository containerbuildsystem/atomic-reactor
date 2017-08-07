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

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_inject_parent_image import InjectParentImage
from atomic_reactor.plugins.exit_remove_built_image import GarbageCollectionPlugin
from atomic_reactor.util import ImageName
from flexmock import flexmock
from tests.constants import MOCK, MOCK_SOURCE
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
def workflow():
    if MOCK:
        mock_docker()
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.builder = MockInsideBuilder()

    flexmock(workflow, base_image_inspect={})

    return workflow


@pytest.fixture()
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
    return session


class TestKojiParent(object):

    def test_parent_image_injected(self, workflow, koji_session):
        previous_parent_image = workflow.builder.base_image
        self.run_plugin_with_args(workflow)
        assert str(previous_parent_image) != str(workflow.builder.base_image)

    @pytest.mark.parametrize('koji_build', (KOJI_BUILD_ID, KOJI_BUILD_NVR, str(KOJI_BUILD_ID)))
    def test_koji_build_identifier(self, workflow, koji_build):
        koji_session(koji_build_id=koji_build)
        self.run_plugin_with_args(workflow, plugin_args={'koji_parent_build': koji_build})

    def test_unknown_koji_build(self, workflow, koji_session):
        unknown_build = KOJI_BUILD_ID + 1
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow, plugin_args={'koji_parent_build': unknown_build})
        assert '{}, not found'.format(unknown_build) in str(exc_info)

    @pytest.mark.parametrize(('repositories', 'selected'), (
        ([':26-3', '@sha256:12345'], '@sha256:12345'),
        ([':26-3', ':26-spam'], ':26-3'),
    ))
    def test_repository_from_koji_build(self, workflow, repositories, selected):
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
        self.run_plugin_with_args(workflow)
        assert str(workflow.builder.base_image) == repo_template.format(selected)

    @pytest.mark.parametrize(('repositories', 'selected'), (
        ([':26-3', '@sha256:12345'], '@sha256:12345'),
        ([':26-3', ':26-spam'], ':26-3'),
    ))
    def test_repository_selection(self, workflow, repositories, selected):
        repo_template = 'spam.com/fedora{}'
        archives = [
            {'id': 1, 'extra': {'docker': {'repositories': [
                repo_template.format(repo) for repo in repositories
            ]}}}
        ]

        koji_session(archives=archives)
        self.run_plugin_with_args(workflow)
        assert str(workflow.builder.base_image) == repo_template.format(selected)

    @pytest.mark.parametrize(('repository', 'is_valid'), (
        ('fedora', True),
        ('rawhide/fedora', False),
        ('centos', False),
    ))
    def test_new_parent_image_validation(self, workflow, repository, is_valid):
        archives = [
            {'id': 1, 'extra': {'docker': {'repositories': [
                'spam.com/{}@sha256:12345'.format(repository),
            ]}}}
        ]

        koji_session(archives=archives)
        if is_valid:
            self.run_plugin_with_args(workflow)
        else:
            with pytest.raises(PluginFailedException) as exc_info:
                self.run_plugin_with_args(workflow)
            assert 'differs from repository for existing parent image' in str(exc_info.value)

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
            .and_return(True)
            .once())
        plugin_args = {'koji_ssl_certs_dir': certs_dir}
        self.run_plugin_with_args(workflow, plugin_args)

    def test_no_archives(self, workflow):
        koji_session(archives=[])
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow)
        assert 'A suitable archive' in str(exc_info.value)
        assert 'not found' in str(exc_info.value)

    def test_no_repositories(self, workflow):
        archives = copy.deepcopy(ARCHIVES)
        for archive in archives:
            graceful_chain_del(archive, 'extra', 'docker', 'repositories')

        koji_session(archives=archives)
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow)
        assert 'A suitable archive' in str(exc_info.value)
        assert 'not found' in str(exc_info.value)

    def run_plugin_with_args(self, workflow, plugin_args=None):
        plugin_args = plugin_args or {}
        plugin_args.setdefault('koji_parent_build', KOJI_BUILD_ID)
        plugin_args.setdefault('koji_hub', KOJI_HUB)

        runner = PreBuildPluginsRunner(
            workflow.builder.tasker,
            workflow,
            [{'name': InjectParentImage.key, 'args': plugin_args}]
        )

        result = runner.run()
        # Koji build ID is always used, even when NVR is given.
        assert result[InjectParentImage.key] == KOJI_BUILD_ID
        self.assert_images_to_remove(workflow)

    def assert_images_to_remove(self, workflow):
        expected = set([str(workflow.builder.base_image)])
        actual = workflow.plugin_workspace[GarbageCollectionPlugin.key]['images_to_remove']
        assert actual == expected
