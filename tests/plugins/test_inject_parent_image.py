"""
Copyright (c) 2017, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import koji

from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_inject_parent_image import InjectParentImage
from atomic_reactor.plugins.exit_remove_built_image import GarbageCollectionPlugin
from atomic_reactor.util import DockerfileImages, graceful_chain_del
from flexmock import flexmock
from tests.util import add_koji_map_in_workflow
from tests.stubs import StubSource

import copy
import pytest


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


@pytest.fixture()
def workflow(workflow):
    workflow.data.dockerfile_images = DockerfileImages(['source_registry.com/fedora:26'])
    workflow.source = StubSource()

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
    def test_parent_image_injected(self, caplog, workflow, base_from_scratch, custom_base_image):
        koji_session()

        wf_data = workflow.data

        if base_from_scratch:
            wf_data.dockerfile_images = DockerfileImages(['scratch'])
        elif custom_base_image:
            wf_data.dockerfile_images = DockerfileImages(['koji/image-build'])

        previous_parent_image = wf_data.dockerfile_images.base_image

        self.run_plugin_with_args(workflow, base_from_scratch=base_from_scratch,
                                  custom_base_image=custom_base_image)
        if base_from_scratch:
            assert str(previous_parent_image) == str(wf_data.dockerfile_images.base_image)

            log_msg = "from scratch can't inject parent image"
            assert log_msg in caplog.text
        elif custom_base_image:
            assert str(previous_parent_image) == str(wf_data.dockerfile_images.base_image)

            log_msg = "custom base image builds can't inject parent image"
            assert log_msg in caplog.text
        else:
            assert str(previous_parent_image) != str(wf_data.dockerfile_images.base_image)

    @pytest.mark.parametrize('koji_build', (KOJI_BUILD_ID, KOJI_BUILD_NVR, str(KOJI_BUILD_ID)))
    def test_koji_build_identifier(self, workflow, koji_build):
        koji_session(koji_build_id=koji_build)
        self.run_plugin_with_args(workflow, plugin_args={'koji_parent_build': koji_build})

    def test_unknown_koji_build(self, workflow):  # noqa
        koji_session()
        unknown_build = KOJI_BUILD_ID + 1
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow, plugin_args={'koji_parent_build': unknown_build})

        assert '{}, not found'.format(unknown_build) in str(exc_info.value)

    @pytest.mark.parametrize('registry_in_koji', ('source_registry.com', 'pull_registry.com'))
    @pytest.mark.parametrize(('repositories', 'selected'), (
        ([':26-3', '@sha256:12345'], '@sha256:12345'),
        ([':26-3', ':26-spam'], ':26-3'),
    ))
    def test_repository_from_koji_build(self, workflow, registry_in_koji, repositories, selected):
        # Populate archives to ensure koji build takes precedence
        archives = [
            {'id': 1, 'extra': {'docker': {'repositories': [
                'spam.com/notselected/fedora{}'.format(repo) for repo in repositories
            ]}}}
        ]

        koji_repo_template = registry_in_koji + '/fedora{}'
        koji_build_info = copy.deepcopy(KOJI_BUILD_INFO)
        koji_build_info['extra'] = {'image': {'index': {'pull': [
            koji_repo_template.format(repo) for repo in repositories
        ]}}}

        repo_template = 'source_registry.com/fedora{}'
        koji_session(archives=archives, koji_build_info=koji_build_info)
        workflow.data.dockerfile_images = DockerfileImages(['spam.com/fedora:some_tag'])
        self.run_plugin_with_args(workflow)
        assert str(workflow.data.dockerfile_images.base_image) == repo_template.format(selected)

    @pytest.mark.parametrize('organization', [None, 'my_organization'])  # noqa
    @pytest.mark.parametrize('archive_registry', ['spam.com', 'old_registry.com'])
    @pytest.mark.parametrize(('repositories', 'selected'), (
        ([':26-3', '@sha256:12345'], '@sha256:12345'),
        ([':26-3', ':26-spam'], ':26-3'),
    ))
    def test_repository_selection(self, workflow, organization, archive_registry,
                                  repositories, selected):
        archive_repo_template = archive_registry + '/fedora{}'
        archives = [
            {'id': 1, 'extra': {'docker': {'repositories': [
                archive_repo_template.format(repo) for repo in repositories
            ]}}}
        ]
        enclosed_repo_template = 'source_registry.com/{}/fedora{}'
        repo_template = 'source_registry.com/fedora{}'

        koji_session(archives=archives)
        workflow.data.dockerfile_images = DockerfileImages(['spam.com/fedora:some_tag'])
        self.run_plugin_with_args(workflow, organization=organization)
        if organization:
            selected_repo = enclosed_repo_template.format(organization, selected)
        else:
            selected_repo = repo_template.format(selected)

        assert str(workflow.data.dockerfile_images.base_image) == selected_repo

    def test_koji_ssl_certs_used(self, tmpdir, workflow):  # noqa
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
        self.run_plugin_with_args(workflow, plugin_args)

    def test_no_archives(self, workflow):  # noqa
        koji_session(archives=[])
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow)
        assert 'A suitable archive' in str(exc_info.value)
        assert 'not found' in str(exc_info.value)

    def test_no_repositories(self, workflow):  # noqa
        archives = copy.deepcopy(ARCHIVES)
        for archive in archives:
            graceful_chain_del(archive, 'extra', 'docker', 'repositories')

        koji_session(archives=archives)
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow)
        assert 'A suitable archive' in str(exc_info.value)
        assert 'not found' in str(exc_info.value)

    def test_skip_build(self, workflow, caplog):  # noqa
        koji_session(archives=[])
        self.run_plugin_with_args(workflow, koji_parent_build='')

        assert 'no koji parent build, skipping plugin' in caplog.text

    def run_plugin_with_args(self, workflow, plugin_args=None, organization=None,
                             base_from_scratch=False, custom_base_image=False,
                             koji_parent_build=KOJI_BUILD_ID):
        plugin_args = plugin_args or {}
        plugin_args.setdefault('koji_parent_build', koji_parent_build)

        rcm = {'version': 1, 'registries_organization': organization,
               'source_registry': {'url': 'source_registry.com'}}
        workflow.conf.conf = rcm
        add_koji_map_in_workflow(workflow,
                                 hub_url=KOJI_HUB,
                                 root_url='',
                                 ssl_certs_dir=plugin_args.get('koji_ssl_certs_dir'))

        runner = PreBuildPluginsRunner(
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
        expected = {str(workflow.data.dockerfile_images.base_image)}
        actual = workflow.data.plugin_workspace[GarbageCollectionPlugin.key]['images_to_remove']
        assert actual == expected
