"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import os
import sys
from copy import deepcopy

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
    PLUGIN_KOJI_PARENT_KEY,
    PLUGIN_CHECK_AND_SET_PLATFORMS_KEY,
    BASE_IMAGE_KOJI_BUILD
)
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.source import SourceConfig
from atomic_reactor.odcs_util import ODCSClient
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins import pre_check_and_set_rebuild
from atomic_reactor.plugins.build_orchestrate_build import (WORKSPACE_KEY_OVERRIDE_KWARGS,
                                                            OrchestrateBuildPlugin)
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY, ReactorConfig)
from atomic_reactor.plugins.pre_resolve_composes import (ResolveComposesPlugin,
                                                         ODCS_DATETIME_FORMAT, UNPUBLISHED_REPOS)

import yaml
from atomic_reactor.util import ImageName, read_yaml
from datetime import datetime, timedelta
from flexmock import flexmock
from tests.constants import MOCK, MOCK_SOURCE
from textwrap import dedent

import logging
import pytest

if MOCK:
    from tests.docker_mock import mock_docker


KOJI_HUB = 'http://koji.com/hub'

KOJI_BUILD_ID = 123456789

KOJI_TAG_NAME = 'test-tag'
KOJI_TARGET_NAME = 'test-target'
KOJI_TARGET = {
    'build_tag_name': KOJI_TAG_NAME,
    'name': KOJI_TARGET_NAME
}

ODCS_URL = 'https://odcs.fedoraproject.org/odcs/1'

ODCS_COMPOSE_ID = 84
ODCS_COMPOSE_REPO = 'https://odcs.fedoraproject.org/composes/latest-odcs-1-1/compose/Temporary'
ODCS_COMPOSE_REPOFILE = ODCS_COMPOSE_REPO + '/odcs-1.repo'
ODCS_COMPOSE_SECONDS_TO_LIVE = timedelta(hours=24)
ODCS_COMPOSE_TIME_TO_EXPIRE = datetime.utcnow() + ODCS_COMPOSE_SECONDS_TO_LIVE
ODCS_COMPOSE_DEFAULT_ARCH = 'x86_64'
ODCS_COMPOSE_DEFAULT_ARCH_LIST = [ODCS_COMPOSE_DEFAULT_ARCH]
ODCS_COMPOSE = {
    'id': ODCS_COMPOSE_ID,
    'result_repo': ODCS_COMPOSE_REPO,
    'result_repofile': ODCS_COMPOSE_REPOFILE,
    'source': KOJI_TAG_NAME,
    'source_type': 'tag',
    'sigkeys': '',
    'state_name': 'done',
    'arches': ODCS_COMPOSE_DEFAULT_ARCH,
    'time_to_expire': ODCS_COMPOSE_TIME_TO_EXPIRE.strftime(ODCS_DATETIME_FORMAT),
}

SIGNING_INTENTS = {
    'release': ['R123'],
    'beta': ['R123', 'B456', 'B457'],
    'unsigned': [],
}

DEFAULT_SIGNING_INTENT = 'release'


class MockInsideBuilder(object):
    def __init__(self, tmpdir):
        self.tasker = DockerTasker()
        self.base_image = ImageName(repo='Fedora', tag='22')
        self.image_id = 'image_id'
        self.image = 'image'
        self.source = MockSource(tmpdir)


@pytest.fixture()
def workflow(tmpdir):
    if MOCK:
        mock_docker()

    buildstep_plugin = [{
        'name': OrchestrateBuildPlugin.key,
        'args': {
            'platforms': ODCS_COMPOSE_DEFAULT_ARCH_LIST
        },
    }]
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image', buildstep_plugins=buildstep_plugin, )
    workflow.builder = MockInsideBuilder(tmpdir)
    workflow.source = workflow.builder.source
    workflow._tmpdir = tmpdir
    workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = set(
                                                                    ODCS_COMPOSE_DEFAULT_ARCH_LIST)
    setattr(workflow.builder, 'base_image_inspect', {})

    mock_reactor_config(workflow, tmpdir)
    mock_repo_config(tmpdir)
    mock_odcs_request()
    workflow._koji_session = mock_koji_session()
    return workflow


class MockSource(object):
    def __init__(self, tmpdir):
        self.dockerfile_path = str(tmpdir.join('Dockerfile'))
        self.path = str(tmpdir)
        self._config = None

    def get_build_file_path(self):
        return self.dockerfile_path, self.path

    @property
    def config(self):  # lazy load after container.yaml has been created
        self._config = self._config or SourceConfig(self.path)
        return self._config


def mock_reactor_config(workflow, tmpdir, data=None, default_si=DEFAULT_SIGNING_INTENT):
    if data is None:
        data = dedent("""\
            version: 1
            odcs:
               signing_intents:
               - name: release
                 keys: ['R123']
               - name: beta
                 keys: ['R123', 'B456', 'B457']
               - name: unsigned
                 keys: []
               default_signing_intent: {}
               api_url: {}
               auth:
                   ssl_certs_dir: {}
            """.format(default_si, ODCS_URL, tmpdir))

    workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

    config = {}
    if data:
        tmpdir.join('cert').write('')
        config = read_yaml(data, 'schemas/config.json')

    workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] = ReactorConfig(config)


def mock_repo_config(tmpdir, data=None, signing_intent=None):
    if data is None:
        data = dedent("""\
            compose:
                packages:
                - spam
                - bacon
                - eggs
            """)
        if signing_intent:
            data += "    signing_intent: {}".format(signing_intent)

    tmpdir.join('container.yaml').write(data)


def mock_content_sets_config(tmpdir, data=None):
    if data is None:
        data = dedent("""\
            x86_64:
            - pulp-spam
            - pulp-bacon
            - pulp-eggs
        """)

    tmpdir.join('content_sets.yml').write(data)


def mock_odcs_request():
    (flexmock(ODCSClient)
        .should_receive('start_compose')
        .with_args(
            source_type='tag',
            source=KOJI_TAG_NAME,
            arches=ODCS_COMPOSE_DEFAULT_ARCH_LIST,
            packages=['spam', 'bacon', 'eggs'],
            sigkeys=['R123'])
        .and_return(ODCS_COMPOSE))

    (flexmock(ODCSClient)
        .should_receive('wait_for_compose')
        .with_args(ODCS_COMPOSE_ID)
        .and_return(ODCS_COMPOSE))


def mock_koji_session():
    koji_session = flexmock()
    flexmock(koji).should_receive('ClientSession').and_return(koji_session)

    def mock_get_build_target(target_name, strict):
        assert strict is True

        if target_name == KOJI_TARGET_NAME:
            return KOJI_TARGET

        raise koji.GenericError('No matching build target found: {}'.format(target_name))

    (flexmock(koji_session)
        .should_receive('getBuildTarget')
        .replace_with(mock_get_build_target))
    (flexmock(koji_session)
        .should_receive('krb_login')
        .and_return(True))

    return koji_session


class TestResolveComposes(object):

    def teardown_method(self, method):
        sys.modules.pop('pre_resolve_composes', None)

    def test_request_compose(self, workflow, reactor_config_map):
        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)

    @pytest.mark.parametrize('arches', (
        ['ppc64le', 'x86_64'],
        ['x86_64'],
    ))
    def test_request_compose_for_multiarch_tag(self, workflow, reactor_config_map, arches):
        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .with_args(
                source_type='tag',
                source='test-tag',
                packages=['spam', 'bacon', 'eggs'],
                sigkeys=['R123'],
                arches=arches)
            .once()
            .and_return(ODCS_COMPOSE))
        workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = arches
        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)

    @pytest.mark.parametrize(('parent_compose', 'parent_repourls', 'repo_provided'), [
        (True, True, False),
        (True, False, False),
        (False, True, False),
        (False, False, False),
        (True, True, True),
        (True, False, True),
        (False, True, True),
        (False, False, True),
    ])
    @pytest.mark.parametrize(('inherit_parent', 'scratch', 'isolated', 'allow_inherit', 'ids',
                              'compose_defined'), [
        (True, True, False, False, True, True),
        (True, False, True, False, True, True),
        (True, False, False, True, True, True),
        (False, True, False, False, True, True),
        (False, False, True, False, True, True),
        (False, False, False, False, True, True),
        (True, True, False, False, False, True),
        (True, False, True, False, False, True),
        (True, False, False, True, False, True),
        (False, True, False, False, False, True),
        (False, False, True, False, False, True),
        (False, False, False, False, False, True),
        (True, True, False, False, True, False),
        (True, False, True, False, True, False),
        (True, False, False, True, True, False),
        (False, True, False, False, True, False),
        (False, False, True, False, True, False),
        (False, False, False, False, True, False),
        (True, True, False, False, False, False),
        (True, False, True, False, False, False),
        (True, False, False, True, False, False),
        (False, True, False, False, False, False),
        (False, False, True, False, False, False),
        (False, False, False, False, False, False),
    ])
    def test_inherit_parents(self, workflow, reactor_config_map, parent_compose, parent_repourls,
                             repo_provided, inherit_parent, scratch, isolated, allow_inherit,
                             compose_defined, ids):
        arches = ['ppc64le', 'x86_64']
        if inherit_parent and compose_defined:
            repo_config = dedent("""\
                compose:
                    packages:
                    - spam
                    - bacon
                    - eggs
                    inherit: true
                """)
            mock_repo_config(workflow._tmpdir, repo_config)
        elif inherit_parent:
            repo_config = dedent("""\
                compose:
                    inherit: true
                """)
            mock_repo_config(workflow._tmpdir, repo_config)
        elif not compose_defined:
            workflow._tmpdir.join('container.yaml').write("")

        new_environ = {}
        new_environ["BUILD"] = dedent('''\
            {
              "metadata": {
                "labels": {}
              }
            }
            ''')

        if scratch:
            new_environ["BUILD"] = dedent('''\
                {
                  "metadata": {
                    "labels": {"scratch": "true"}
                  }
                }
                ''')
        elif isolated:
            new_environ["BUILD"] = dedent('''\
               {
                 "metadata": {
                   "labels": {"isolated": "true"}
                 }
               }
               ''')
        flexmock(os)
        os.should_receive("environ").and_return(new_environ)  # pylint: disable=no-member

        parent_compose_ids = [10, 11]
        parent_repo = ["http://example.com/parent.repo", ]
        parent_build_info = {
            'id': 1234,
            'nvr': 'fedora-27-1',
            'extra': {'image': {'odcs': {'compose_ids': parent_compose_ids,
                                         'signing_intent': 'unsigned'},
                                'yum_repourls': parent_repo}},
        }
        if not parent_repourls:
            parent_build_info['extra']['image'].pop('yum_repourls')
        if not parent_compose:
            parent_build_info['extra']['image'].pop('odcs')

        workflow.prebuild_results[PLUGIN_KOJI_PARENT_KEY] = {
            BASE_IMAGE_KOJI_BUILD: parent_build_info,
        }

        if ids:
            (flexmock(ODCSClient)
             .should_receive('start_compose')
             .never())
        elif compose_defined:
            sigkeys = []
            if not parent_compose:
                sigkeys = ['R123']
            (flexmock(ODCSClient)
                .should_receive('start_compose')
                .with_args(
                    source_type='tag',
                    source='test-tag',
                    packages=['spam', 'bacon', 'eggs'],
                    sigkeys=sigkeys,
                    arches=arches)
                .once()
                .and_return(ODCS_COMPOSE))

            (flexmock(ODCSClient)
                .should_receive('wait_for_compose')
                .with_args(ODCS_COMPOSE_ID)
                .and_return(ODCS_COMPOSE))

        compose_ids = []
        current_repourl = ["http://example.com/current.repo"]
        expected_yum_repourls = []
        if repo_provided:
            expected_yum_repourls = list(current_repourl)
        if not ids and compose_defined:
            expected_yum_repourls.append(ODCS_COMPOSE['result_repofile'])
        if allow_inherit and parent_repourls:
            expected_yum_repourls.extend(parent_repo)

        if ids:
            for compose_id in range(3, 6):
                compose = ODCS_COMPOSE.copy()
                compose['id'] = compose_id
                compose['result_repofile'] = ODCS_COMPOSE_REPO + '/odcs-{}.repo'.format(compose_id)

                (flexmock(ODCSClient)
                    .should_receive('wait_for_compose')
                    .once()
                    .with_args(compose_id)
                    .and_return(compose))

                compose_ids.append(compose_id)
                expected_yum_repourls.append(compose['result_repofile'])

        if allow_inherit and parent_compose:
            for compose_id in parent_compose_ids:
                compose = ODCS_COMPOSE.copy()
                compose['id'] = compose_id
                compose['result_repofile'] = ODCS_COMPOSE_REPO + '/odcs-{}.repo'.format(compose_id)

                (flexmock(ODCSClient)
                    .should_receive('wait_for_compose')
                    .once()
                    .with_args(compose_id)
                    .and_return(compose))
                expected_yum_repourls.append(compose['result_repofile'])

        workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = arches

        plugin_args = {}
        if repo_provided:
            plugin_args['repourls'] = current_repourl
        if ids:
            plugin_args['compose_ids'] = compose_ids

        self.run_plugin_with_args(workflow, plugin_args, reactor_config_map=reactor_config_map,
                                  check_for_default_id=False)

        archspecific_repuruls = self.get_override_yum_repourls(workflow)
        nonearch_repourls = self.get_override_yum_repourls(workflow, arch=None)

        if compose_defined or ids or (parent_compose and allow_inherit):
            assert set(archspecific_repuruls) == set(expected_yum_repourls)
        else:
            assert set(nonearch_repourls) == set(expected_yum_repourls)

        all_yum_repourls = []
        if repo_provided:
            all_yum_repourls = list(current_repourl)
        if allow_inherit and parent_repourls:
            all_yum_repourls.extend(parent_repo)
        assert set(workflow.all_yum_repourls) == set(all_yum_repourls)

    @pytest.mark.parametrize('arches', (
        ['ppc64le', 'x86_64'],
        ['x86_64'],
    ))
    def test_request_compose_for_modules(self, workflow, reactor_config_map, arches):
        repo_config = dedent("""\
            compose:
                modules:
                - spam
                - bacon
                - eggs
            """)
        mock_repo_config(workflow._tmpdir, repo_config)

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .with_args(
                source_type='module',
                source='spam bacon eggs',
                sigkeys=['R123'],
                arches=arches)
            .once()
            .and_return(ODCS_COMPOSE))
        workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = arches
        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)

    @pytest.mark.parametrize('arches', (
        ['ppc64le', 'x86_64'],
        ['x86_64'],
    ))
    def test_request_compose_for_modular_tags(self, workflow, reactor_config_map, arches):
        repo_config = dedent("""\
            compose:
                modules:
                - spam
                - bacon
                - eggs
                modular_koji_tags:
                - earliest
                - latest
            """)
        mock_repo_config(workflow._tmpdir, repo_config)

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .with_args(
                source_type='module',
                source='spam bacon eggs',
                sigkeys=['R123'],
                arches=arches,
                modular_koji_tags=['earliest', 'latest'])
            .once()
            .and_return(ODCS_COMPOSE))
        workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = arches
        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)

    @pytest.mark.parametrize(('with_modules'), (True, False))
    def test_request_compose_empty_packages(self, workflow, reactor_config_map, with_modules):
        repo_config = dedent("""\
            compose:
                packages:
            """)
        if with_modules:
            repo_config = dedent("""\
                compose:
                    packages:
                    modules:
                    - spam_modules
                    - bacon_modules
                    - eggs_modules
                """)
        mock_repo_config(workflow._tmpdir, repo_config)

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .with_args(source_type='tag',
                       source='test-tag',
                       sigkeys=['R123'],
                       packages=None,
                       arches=['x86_64'])
            .and_return(ODCS_COMPOSE))
        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .with_args(source_type='module',
                       source='spam_modules bacon_modules eggs_modules',
                       sigkeys=['R123'],
                       arches=['x86_64'])
            .and_return(ODCS_COMPOSE))

        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)

    @pytest.mark.parametrize(('compose_arches', 'pulp_arches', 'multilib_arches',
                              'request_multilib'), [
        (['i686'], None, None, None),
        (['i686'], None, ['i686'], ['i686']),
        (['i686'], None, ['ppc64le'], None),
        (['i686'], None, ['s390x', 'i686', 'ppc64le'], ['i686']),
        (['i686', 'ppc64le'], None, ['s390x', 'i686', 'ppc64le'], ['i686', 'ppc64le']),
        (['i686'], ['ppc64le'], None, None),
        (['i686'], ['ppc64le'], ['i686'], ['i686']),
        # pcc64le is in the pulp list but not the compose list, so it's not built at all
        (['i686'], ['ppc64le'], ['ppc64le'], None),
        (['i686'], ['ppc64le'], ['s390x', 'i686', 'ppc64le'], ['i686']),
        (['i686', 'ppc64le'], ['ppc64le'], ['s390x', 'i686', 'ppc64le'],
         ['i686', 'ppc64le']),
    ])
    @pytest.mark.parametrize(('multilib_method', 'method_results'), [
        (["none"], ['none']),
        (["devel"], ['devel']),
        (["runtime"], ['runtime']),
        (["all"], ['all']),
        (["runtime", "devel"], ['devel', 'runtime']),
        (None, []),
    ])
    def test_multilib(self, workflow, reactor_config_map,
                      compose_arches, pulp_arches, multilib_arches, request_multilib,
                      multilib_method, method_results):
        base_repos = ['spam', 'bacon', 'eggs']

        content_dict = {}
        for arch in pulp_arches or []:
            pulp_repos = []
            for repo in base_repos:
                pulp_repos.append(repo + '-' + arch)
            content_dict[arch] = pulp_repos

        mock_content_sets_config(workflow._tmpdir, yaml.safe_dump(content_dict))

        repo_config = {
            'compose': {
                'packages': base_repos
            }
        }
        if multilib_arches:
            repo_config['compose']['multilib_arches'] = multilib_arches
        if multilib_method:
            repo_config['compose']['multilib_method'] = multilib_method
        if pulp_arches:
            repo_config['compose']['pulp_repos'] = True

        mock_repo_config(workflow._tmpdir, yaml.safe_dump(repo_config))
        if compose_arches:
            workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = set(compose_arches)
        else:
            del workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY]

        reactor_conf =\
            deepcopy(workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY].conf)
        reactor_conf['koji'] = {'hub_url': KOJI_HUB, 'root_url': '', 'auth': {}}

        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig(reactor_conf)

        # just confirm that render_requests is returning valid data, without the overhead of
        # mocking the compose results
        plugin = ResolveComposesPlugin(workflow.builder.tasker, workflow,
                                       odcs_url=ODCS_URL,
                                       koji_target=KOJI_TARGET_NAME, koji_hub=KOJI_HUB)
        plugin.read_configs()
        plugin.adjust_compose_config()
        composed_arches = set([])
        composes = plugin.compose_config.render_requests()
        for compose_config in composes:
            composed_arches.update(compose_config['arches'])
            if request_multilib:
                if compose_config['source_type'] == 'tag':
                    assert sorted(compose_config['multilib_arches']) == sorted(request_multilib)
                    compose_methods = compose_config['multilib_method'] or []
                    assert sorted(compose_methods) == sorted(method_results)
                    continue
                else:
                    if compose_config['arches'][0] in request_multilib:
                        assert compose_config['multilib_arches'] == compose_config['arches']
                        compose_methods = compose_config['multilib_method'] or []
                        assert sorted(compose_methods) == sorted(method_results)
                        continue
            # fall through if multilib wasn't requested or if the pulp arch wasn't in
            # the multilib request
            assert 'multilib_arches' not in compose_config
            assert 'multilib_method' not in compose_config
        assert composed_arches == set(compose_arches)

    @pytest.mark.parametrize(('pulp_arches', 'arches', 'signing_intent', 'expected_intent'), (
        (None, None, 'unsigned', 'unsigned'),
        # For the next test, since arches is none, no compose is performed even though pulp_arches
        # has a value. Expected intent doesn't change when nothing is composed.
        (['x86_64'], None, 'release', 'release'),
        # pulp composes have the beta signing intent and downgrade the release intent to beta.
        (['x86_64'], ['x86_64'], 'release', 'beta'),
        (['x86_64', 'ppce64le'], ['x86_64', 'ppce64le'], 'release', 'beta'),
        (['x86_64', 'ppce64le'], ['x86_64'], 'release', 'beta'),
        (['x86_64', 'ppce64le', 'arm64'], ['x86_64', 'ppce64le', 'arm64'], 'beta', 'beta'),
        # pulp composes have the beta signing intent but the unsigned intent overrides that
        (['x86_64', 'ppce64le', 'arm64'], ['x86_64', 'ppce64le', 'arm64'], 'unsigned', 'unsigned'),
        # For the next test, since arches is none, no compose is performed even though pulp_arches
        # has a value. Expected intent doesn't change when nothing is composed.
        (['x86_64', 'ppce64le', 'arm64'], None, 'beta', 'beta'),
    ))
    @pytest.mark.parametrize(('flags', 'expected_flags'), [
        ({}, None),
        ({UNPUBLISHED_REPOS: False}, None),
        ({UNPUBLISHED_REPOS: True}, [UNPUBLISHED_REPOS])
    ])
    def test_request_pulp_and_multiarch(self, workflow, reactor_config_map, pulp_arches, arches,
                                        signing_intent, expected_intent, flags, expected_flags):
        content_set = ''
        pulp_composes = {}
        base_repos = ['spam', 'bacon', 'eggs']
        pulp_id = ODCS_COMPOSE_ID
        arches = arches or []

        for arch in pulp_arches or []:
            pulp_id += 1
            pulp_repos = []
            content_set += """\n    {0}:""".format(arch)
            for repo in base_repos:
                pulp_repo = repo + '-' + arch
                pulp_repos.append(pulp_repo)
                content_set += """\n    - {0}""".format(pulp_repo)
            source = ' '.join(pulp_repos)

            if arch not in arches:
                continue

            pulp_compose = {
                'id': pulp_id,
                'result_repo': ODCS_COMPOSE_REPO,
                'result_repofile': ODCS_COMPOSE_REPO + '/pulp_compose-' + arch,
                'source': source,
                'source_type': 'pulp',
                'sigkeys': "B457",
                'state_name': 'done',
                'arches': arch,
                'time_to_expire': ODCS_COMPOSE_TIME_TO_EXPIRE.strftime(ODCS_DATETIME_FORMAT),
            }
            pulp_composes[arch] = pulp_compose
            if expected_flags:
                pulp_composes['flags'] = expected_flags

            (flexmock(ODCSClient)
                .should_receive('start_compose')
                .with_args(source_type='pulp', source=source, arches=[arch], sigkeys=[],
                           flags=expected_flags)
                .and_return(pulp_composes[arch]).once())
            (flexmock(ODCSClient)
                .should_receive('wait_for_compose')
                .with_args(pulp_id)
                .and_return(pulp_composes[arch]).once())

        mock_content_sets_config(workflow._tmpdir, content_set)

        repo_config = dedent("""\
            compose:
                pulp_repos: true
                packages:
                - spam
                - bacon
                - eggs
                signing_intent: {0}
            """.format(signing_intent))
        for flag in flags:
            repo_config += ("    {0}: {1}\n".format(flag, flags[flag]))
        mock_repo_config(workflow._tmpdir, repo_config)
        if arches:
            workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = set(arches)
        else:
            del workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY]
        tag_compose = deepcopy(ODCS_COMPOSE)

        sig_keys = SIGNING_INTENTS[signing_intent]
        tag_compose['sigkeys'] = ' '.join(sig_keys)
        if arches:
            tag_compose['arches'] = ' '.join(arches)
            (flexmock(ODCSClient)
                .should_receive('start_compose')
                .with_args(source_type='tag', source=KOJI_TAG_NAME, arches=sorted(arches),
                           packages=['spam', 'bacon', 'eggs'], sigkeys=sig_keys)
                .and_return(tag_compose).once())
        else:
            tag_compose.pop('arches')
            (flexmock(ODCSClient)
                .should_receive('start_compose')
                .with_args(source_type='tag', source=KOJI_TAG_NAME,
                           packages=['spam', 'bacon', 'eggs'], sigkeys=sig_keys)
                .and_return(tag_compose).once())

        (flexmock(ODCSClient)
            .should_receive('wait_for_compose')
            .with_args(ODCS_COMPOSE_ID)
            .and_return(tag_compose).once())

        plugin_result = self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map,
                                                  platforms=arches, is_pulp=pulp_arches)

        assert plugin_result['signing_intent'] == expected_intent

    def test_invalid_flag(self, workflow, reactor_config_map):
        expect_error = "Additional properties are not allowed ('some_invalid_flag' was unexpected)"
        arches = ['x86_64']
        repo_config = dedent("""\
            compose:
                pulp_repos: true
                packages:
                - spam
                - bacon
                - eggs
                signing_intent: unsigned
                some_invalid_flag: true
            """)
        mock_repo_config(workflow._tmpdir, repo_config)
        workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = set(arches)
        with pytest.raises(PluginFailedException) as exc:
            self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map,
                                      platforms=arches, is_pulp=False)
        assert expect_error in str(exc.value)

    def test_request_compose_for_pulp_no_content_sets(self, workflow, reactor_config_map):
        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .with_args(
                source_type='pulp',
                source='pulp is no good here',
                arches=['x86_64'],
                sigkeys=[])
            .never())
        (flexmock(ODCSClient)
            .should_receive('wait_for_compose')
            .with_args(85)
            .never())

        mock_content_sets_config(workflow._tmpdir, '')

        repo_config = dedent("""\
            compose:
                pulp_repos: true
                packages:
                - spam
                - bacon
                - eggs
            """)
        mock_repo_config(workflow._tmpdir, repo_config)
        mock_odcs_request()

        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)

    def test_signing_intent_and_compose_ids_mutex(self, workflow, reactor_config_map):
        plugin_args = {'compose_ids': [1, 2], 'signing_intent': 'unsigned'}
        self.run_plugin_with_args(workflow, plugin_args,
                                  expect_error='cannot be used at the same time',
                                  reactor_config_map=reactor_config_map)

    @pytest.mark.parametrize(('plugin_args', 'expected_kwargs'), (
        ({
            'odcs_insecure': True,
        }, {'insecure': True}),

        ({
            'odcs_insecure': False,
        }, {'insecure': False}),

        ({
            'odcs_openidc_secret_path': True,
        }, {'token': 'the-token', 'insecure': False}),

        ({
            'odcs_ssl_secret_path': True,
        }, {'cert': '<tbd-cert-path>', 'insecure': False}),

        ({
            'odcs_ssl_secret_path': 'non-existent-path',
        }, {'insecure': False}),

    ))
    def test_odcs_session_creation(self, tmpdir, workflow, reactor_config_map,
                                   plugin_args, expected_kwargs):
        plug_args = deepcopy(plugin_args)
        exp_kwargs = deepcopy(expected_kwargs)
        mock_reactor_config(workflow, tmpdir)
        has_ssl_path = False
        has_open_path = False

        if plug_args.get('odcs_openidc_secret_path') is True:
            has_open_path = True
            workflow._tmpdir.join('token').write('the-token')
            plug_args['odcs_openidc_secret_path'] = str(workflow._tmpdir)

        if plug_args.get('odcs_ssl_secret_path') is True:
            has_ssl_path = True
            workflow._tmpdir.join('cert').write('the-cert')
            plug_args['odcs_ssl_secret_path'] = str(workflow._tmpdir)
            exp_kwargs['cert'] = str(workflow._tmpdir.join('cert'))

        reac_conf = workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY].conf
        if reactor_config_map:
            exp_kwargs['insecure'] = False
            if 'token' in exp_kwargs:
                reac_conf['odcs']['auth'].pop('ssl_certs_dir')
                reac_conf['odcs']['auth']['openidc_dir'] = str(workflow._tmpdir)
            else:
                exp_kwargs['cert'] = os.path.join(reac_conf['odcs']['auth']['ssl_certs_dir'],
                                                  'cert')
        else:
            if has_ssl_path:
                reac_conf['odcs']['auth']['ssl_certs_dir'] = str(workflow._tmpdir)
            else:
                reac_conf['odcs']['auth'].pop('ssl_certs_dir')
            if has_open_path:
                reac_conf['odcs']['auth']['openidc_dir'] = str(workflow._tmpdir)
            reac_conf['odcs']['insecure'] = plugin_args.get('odcs_insecure', False)

        (flexmock(ODCSClient)
            .should_receive('__init__')
            .with_args(ODCS_URL, **exp_kwargs))

        self.run_plugin_with_args(workflow, plug_args, reactor_config_map=reactor_config_map)

    @pytest.mark.parametrize(('plugin_args', 'ssl_login'), (
        ({
            'koji_target': KOJI_TARGET_NAME,
            'koji_hub': KOJI_BUILD_ID,
            'koji_ssl_certs_dir': '/path/to/certs',
        }, True),

        ({
            'koji_target': KOJI_TARGET_NAME,
            'koji_hub': KOJI_BUILD_ID,
        }, False),
    ))
    def test_koji_session_creation(self, workflow, plugin_args, ssl_login, reactor_config_map):
        koji_session = workflow._koji_session

        (flexmock(koji_session)
            .should_receive('ssl_login')
            .times(int(ssl_login))
            .and_return(True))

        (flexmock(koji_session)
            .should_receive('getBuildTarget')
            .once()
            .with_args(plugin_args['koji_target'], strict=True)
            .and_return(KOJI_TARGET))

        self.run_plugin_with_args(workflow, plugin_args, reactor_config_map=reactor_config_map)

    def test_koji_hub_requirement(self, workflow):
        plugin_args = {'koji_target': 'test-target', 'koji_hub': None}
        self.run_plugin_with_args(workflow, plugin_args,
                                  expect_error='koji_hub is required when koji_target is used')

    @pytest.mark.parametrize(('default_si', 'config_si', 'arg_si', 'parent_si', 'expected_si',
                              'overridden'), (
        # Downgraded by parent's signing intent
        ('release', None, None, 'beta', 'beta', True),
        ('beta', None, None, 'unsigned', 'unsigned', True),
        ('release', 'release', None, 'beta', 'beta', True),
        ('release', 'beta', None, 'unsigned', 'unsigned', True),

        # Not upgraded by parent's signing intent
        ('release', 'beta', None, 'release', 'beta', False),
        ('release', 'beta', 'beta', 'release', 'beta', False),

        # Downgraded by signing_intent plugin argument
        ('release', 'release', 'beta', 'release', 'beta', True),
        ('release', 'release', 'beta', None, 'beta', True),

        # Upgraded by signing_intent plugin argument
        ('release', 'beta', 'release', 'release', 'release', True),
        ('release', 'beta', 'release', None, 'release', True),

        # Upgraded by signing_intent plugin argument but capped by parent's signing intent
        ('beta', 'beta', 'release', 'unsigned', 'unsigned', True),
        ('beta', 'beta', 'release', 'beta', 'beta', False),
        ('release', 'beta', 'beta', 'unsigned', 'unsigned', True),

        # Modified by repo config
        ('release', 'unsigned', None, None, 'unsigned', False),
        ('unsigned', 'release', None, None, 'release', False),

        # Environment default signing intent used as is
        ('release', None, None, None, 'release', False),
        ('beta', None, None, None, 'beta', False),
        ('unsigned', None, None, None, 'unsigned', False),

    ))
    @pytest.mark.parametrize('use_compose_id', (False, True))
    def test_adjust_signing_intent(self, tmpdir, workflow, default_si, config_si, arg_si,
                                   parent_si, expected_si, overridden, use_compose_id,
                                   reactor_config_map):

        mock_reactor_config(workflow, tmpdir, default_si=default_si)
        mock_repo_config(workflow._tmpdir, signing_intent=config_si)

        sigkeys = SIGNING_INTENTS[expected_si]
        odcs_compose = ODCS_COMPOSE.copy()
        odcs_compose['sigkeys'] = ' '.join(sigkeys)

        arg_compose_ids = []
        if use_compose_id and arg_si:
            # Swap out signing_intent plugin argument with compose_ids.
            # Set mocks to return pre-existing compose instead.
            arg_compose_ids = [ODCS_COMPOSE_ID]
            sigkeys = SIGNING_INTENTS[arg_si]
            odcs_compose['sigkeys'] = sigkeys
            arg_si = None

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .times(0 if arg_compose_ids else 1)
            .with_args(
                source_type='tag',
                source=KOJI_TAG_NAME,
                packages=['spam', 'bacon', 'eggs'],
                arches=ODCS_COMPOSE_DEFAULT_ARCH_LIST,
                sigkeys=sigkeys)
            .and_return(odcs_compose))

        (flexmock(ODCSClient)
            .should_receive('wait_for_compose')
            .once()
            .with_args(odcs_compose['id'])
            .and_return(odcs_compose))

        parent_build_info = {
            'id': 1234,
            'nvr': 'fedora-27-1',
            'extra': {'image': {}},
        }
        if parent_si:
            parent_build_info['extra']['image'] = {'odcs': {'signing_intent': parent_si}}

        workflow.prebuild_results[PLUGIN_KOJI_PARENT_KEY] = {
            BASE_IMAGE_KOJI_BUILD: parent_build_info,
        }

        plugin_args = {}
        if arg_si:
            plugin_args['signing_intent'] = arg_si
        if arg_compose_ids:
            plugin_args['compose_ids'] = arg_compose_ids

        plugin_result = self.run_plugin_with_args(workflow, plugin_args,
                                                  reactor_config_map=reactor_config_map)
        expected_result = {
            'signing_intent': expected_si,
            'signing_intent_overridden': overridden,
            'composes': [odcs_compose],
        }
        assert plugin_result == expected_result

    @pytest.mark.parametrize(('composes_intent', 'expected_intent'), (
        (('release', 'beta'), 'beta'),
        (('beta', 'release'), 'beta'),
        (('release', 'release'), 'release'),
        (('unsigned', 'release'), 'unsigned'),
    ))
    def test_signing_intent_multiple_composes(self, workflow, composes_intent, expected_intent,
                                              reactor_config_map):
        composes = []

        for compose_id, signing_intent in enumerate(composes_intent):
            compose = ODCS_COMPOSE.copy()
            compose['id'] = compose_id
            compose['sigkeys'] = ' '.join(SIGNING_INTENTS[signing_intent])

            (flexmock(ODCSClient)
                .should_receive('wait_for_compose')
                .once()
                .with_args(compose_id)
                .and_return(compose))

            composes.append(compose)

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .never())

        plugin_args = {'compose_ids': [item['id'] for item in composes]}
        plugin_result = self.run_plugin_with_args(workflow, plugin_args,
                                                  reactor_config_map=reactor_config_map)

        assert plugin_result['signing_intent'] == expected_intent
        assert plugin_result['composes'] == composes

    @pytest.mark.parametrize(('config', 'error_message'), (
        (dedent("""\
            compose:
                modules: []
            """), 'Nothing to compose'),

        (dedent("""\
            compose:
                pulp_repos: true
            """), 'Nothing to compose'),
    ))
    def test_invalid_compose_request(self, workflow, config, error_message,
                                     reactor_config_map):
        mock_repo_config(workflow._tmpdir, config)
        self.run_plugin_with_args(workflow, expect_error=error_message,
                                  reactor_config_map=reactor_config_map)

    def test_empty_compose_request(self, caplog, workflow, reactor_config_map):
        config = dedent("""\
            compose:
            """)
        mock_repo_config(workflow._tmpdir, config)
        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)
        msg = 'Aborting plugin execution: "compose" config not set and compose_ids not given'
        assert msg in (x.message for x in caplog.records)

    def test_only_pulp_repos(self, workflow, reactor_config_map):
        mock_repo_config(workflow._tmpdir,
                         dedent("""\
                             compose:
                                 pulp_repos: true
                             """))
        mock_content_sets_config(workflow._tmpdir)
        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .with_args(
                source_type='pulp',
                source='pulp-spam pulp-bacon pulp-eggs',
                sigkeys=[],
                flags=None,
                arches=['x86_64'])
            .and_return(ODCS_COMPOSE))
        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)

    @pytest.mark.parametrize(('state_name', 'time_to_expire_delta', 'expect_renew'), (
        ('removed', timedelta(), True),
        ('removed', timedelta(hours=-2), True),
        ('done', timedelta(), True),
        # Grace period to avoid timing issues during test runs
        ('done', timedelta(minutes=118), True),
        ('done', timedelta(hours=3), False),
    ))
    def test_renew_compose(self, workflow, state_name, time_to_expire_delta, expect_renew,
                           reactor_config_map):
        old_odcs_compose = ODCS_COMPOSE.copy()
        time_to_expire = (ODCS_COMPOSE_TIME_TO_EXPIRE -
                          ODCS_COMPOSE_SECONDS_TO_LIVE +
                          time_to_expire_delta)
        old_odcs_compose.update({
            'state_name': state_name,
            'time_to_expire': time_to_expire.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

        new_odcs_compose = ODCS_COMPOSE.copy()
        new_odcs_compose.update({
            'id': old_odcs_compose['id'] + 1
        })

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .never())

        (flexmock(ODCSClient)
            .should_receive('wait_for_compose')
            .once()
            .with_args(old_odcs_compose['id'])
            .and_return(old_odcs_compose))

        (flexmock(ODCSClient)
            .should_receive('renew_compose')
            .times(1 if expect_renew else 0)
            .with_args(old_odcs_compose['id'])
            .and_return(new_odcs_compose))

        (flexmock(ODCSClient)
            .should_receive('wait_for_compose')
            .times(1 if expect_renew else 0)
            .with_args(new_odcs_compose['id'])
            .and_return(new_odcs_compose))

        plugin_args = {
            'compose_ids': [old_odcs_compose['id']],
            'minimum_time_to_expire': timedelta(hours=2).total_seconds(),
        }
        plugin_result = self.run_plugin_with_args(workflow, plugin_args,
                                                  reactor_config_map=reactor_config_map)

        if expect_renew:
            assert plugin_result['composes'] == [new_odcs_compose]
        else:
            assert plugin_result['composes'] == [old_odcs_compose]

    def test_inject_yum_repos_from_new_compose(self, workflow, reactor_config_map):
        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)
        assert self.get_override_yum_repourls(workflow) == [ODCS_COMPOSE_REPOFILE]

    def test_inject_yum_repos_from_existing_composes(self, workflow, reactor_config_map):
        compose_ids = []
        expected_yum_repourls = []

        for compose_id in range(3):
            compose = ODCS_COMPOSE.copy()
            compose['id'] = compose_id
            compose['result_repofile'] = ODCS_COMPOSE_REPO + '/odcs-{}.repo'.format(compose_id)

            (flexmock(ODCSClient)
                .should_receive('wait_for_compose')
                .once()
                .with_args(compose_id)
                .and_return(compose))

            compose_ids.append(compose_id)
            expected_yum_repourls.append(compose['result_repofile'])

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .never())

        plugin_args = {'compose_ids': compose_ids}
        self.run_plugin_with_args(workflow, plugin_args, reactor_config_map=reactor_config_map)

        assert self.get_override_yum_repourls(workflow) == expected_yum_repourls

    def test_abort_when_odcs_config_missing(self, tmpdir, caplog, workflow, reactor_config_map):
        # Clear out default reactor config
        mock_reactor_config(workflow, tmpdir, data='')
        with caplog.at_level(logging.INFO):
            self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)

        msg = 'Aborting plugin execution: ODCS config not found'
        assert msg in (x.message for x in caplog.records)

    def test_abort_when_compose_config_missing(self, caplog, workflow, reactor_config_map):
        # Clear out default git repo config
        mock_repo_config(workflow._tmpdir, '')
        # Ensure no compose_ids are passed to plugin
        plugin_args = {'compose_ids': tuple()}
        with caplog.at_level(logging.INFO):
            self.run_plugin_with_args(workflow, plugin_args, reactor_config_map=reactor_config_map)

        msg = 'Aborting plugin execution: "compose" config not set and compose_ids not given'
        assert msg in (x.message for x in caplog.records)

    def test_invalid_koji_build_target(self, workflow, reactor_config_map):
        plugin_args = {
            'koji_hub': KOJI_HUB,
            'koji_target': 'spam',
        }
        expect_error = 'No matching build target found'
        self.run_plugin_with_args(workflow, plugin_args, expect_error=expect_error,
                                  reactor_config_map=reactor_config_map)

    @pytest.mark.parametrize(('plugin_args', 'msg'), (
        ({'signing_intent': 'spam'},
         'Autorebuild detected: Ignoring signing_intent plugin parameter'),

        ({'compose_ids': [1, 2, 3]},
         'Autorebuild detected: Ignoring compose_ids plugin parameter'),
    ))
    def test_parameters_ignored_for_autorebuild(self, caplog, workflow, plugin_args, msg,
                                                reactor_config_map):
        flexmock(pre_check_and_set_rebuild).should_receive('is_rebuild').and_return(True)
        with caplog.at_level(logging.INFO):
            self.run_plugin_with_args(workflow, plugin_args,
                                      reactor_config_map=reactor_config_map)

        assert msg in (x.message for x in caplog.records)

    def run_plugin_with_args(self, workflow, plugin_args=None,
                             expect_error=None, reactor_config_map=False,
                             platforms=ODCS_COMPOSE_DEFAULT_ARCH_LIST, is_pulp=None,
                             check_for_default_id=True):
        plugin_args = plugin_args or {}
        plugin_args.setdefault('odcs_url', ODCS_URL)
        plugin_args.setdefault('koji_target', KOJI_TARGET_NAME)
        plugin_args.setdefault('koji_hub', KOJI_HUB)
        reactor_conf =\
            deepcopy(workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY].conf)

        if reactor_config_map:
            reactor_conf['koji'] = {'hub_url': KOJI_HUB, 'root_url': '', 'auth': {}}
            if 'koji_ssl_certs_dir' in plugin_args:
                reactor_conf['koji']['auth']['ssl_certs_dir'] = plugin_args['koji_ssl_certs_dir']
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig(reactor_conf)

        runner = PreBuildPluginsRunner(
            workflow.builder.tasker,
            workflow,
            [
                {'name': ResolveComposesPlugin.key, 'args': plugin_args},
            ]
        )

        if expect_error:
            with pytest.raises(PluginFailedException) as exc_info:
                runner.run()
            assert expect_error in str(exc_info.value)
            return

        results = runner.run()[ResolveComposesPlugin.key]
        if results:
            for platform in platforms or []:
                yum_repourls = self.get_override_yum_repourls(workflow, platform)
                # Koji tag compose is present in each one
                if check_for_default_id:
                    assert ODCS_COMPOSE['result_repofile'] in yum_repourls
                if is_pulp:
                    pulp_repo = ODCS_COMPOSE_REPO + '/pulp_compose-' + platform
                    assert pulp_repo in yum_repourls
            yum_repourls = self.get_override_yum_repourls(workflow, None)
            if platforms:
                assert yum_repourls is None
            else:
                assert ODCS_COMPOSE['result_repofile'] in yum_repourls
            assert set(results.keys()) == set(['signing_intent', 'signing_intent_overridden',
                                               'composes'])
        else:
            assert self.get_override_yum_repourls(workflow) is None
            assert results is None
        return results

    def get_override_yum_repourls(self, workflow, arch=ODCS_COMPOSE_DEFAULT_ARCH):
        return (workflow.plugin_workspace
                .get(OrchestrateBuildPlugin.key, {})
                .get(WORKSPACE_KEY_OVERRIDE_KWARGS, {})
                .get(arch, {})
                .get('yum_repourls'))
