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

from atomic_reactor.constants import PLUGIN_KOJI_PARENT_KEY
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.odcs_util import ODCSClient
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins import pre_check_and_set_rebuild
from atomic_reactor.plugins.build_orchestrate_build import (WORKSPACE_KEY_OVERRIDE_KWARGS,
                                                            OrchestrateBuildPlugin)
from atomic_reactor.plugins.pre_reactor_config import ReactorConfigPlugin
from atomic_reactor.plugins.pre_resolve_composes import ResolveComposesPlugin, ODCS_DATETIME_FORMAT
from atomic_reactor.util import ImageName
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
ODCS_COMPOSE = {
    'id': ODCS_COMPOSE_ID,
    'result_repo': ODCS_COMPOSE_REPO,
    'result_repofile': ODCS_COMPOSE_REPOFILE,
    'source': KOJI_TAG_NAME,
    'source_type': 'tag',
    'sigkeys': '',
    'state_name': 'done',
    'time_to_expire': ODCS_COMPOSE_TIME_TO_EXPIRE.strftime(ODCS_DATETIME_FORMAT)
}

SIGNING_INTENTS = {
    'release': ['R123'],
    'beta': ['R123', 'B456'],
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
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.builder = MockInsideBuilder(tmpdir)
    workflow.source = workflow.builder.source
    workflow._tmpdir = tmpdir

    flexmock(workflow, base_image_inspect={})

    mock_reactor_config(tmpdir)
    mock_repo_config(tmpdir)
    mock_odcs_request()
    workflow._koji_session = mock_koji_session()
    return workflow


class MockSource(object):
    def __init__(self, tmpdir):
        self.dockerfile_path = str(tmpdir.join('Dockerfile'))
        self.path = str(tmpdir)

    def get_build_file_path(self):
        return self.dockerfile_path, self.path


def mock_reactor_config(tmpdir, data=None, default_si=DEFAULT_SIGNING_INTENT):
    if data is None:
        data = dedent("""\
            version: 1
            odcs:
               signing_intents:
               - name: release
                 keys: ['R123']
               - name: beta
                 keys: ['R123', 'B456']
               - name: unsigned
                 keys: []
               default_signing_intent: {}
            """.format(default_si))

    tmpdir.join('config.yaml').write(data)


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


def mock_odcs_request():
    (flexmock(ODCSClient)
        .should_receive('start_compose')
        .with_args(
            source_type='tag',
            source=KOJI_TAG_NAME,
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

    return koji_session


class TestResolveComposes(object):

    def test_request_compose(self, workflow):
        self.run_plugin_with_args(workflow)

    def test_request_compose_for_modules(self, workflow):
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
                sigkeys=['R123'])
            .and_return(ODCS_COMPOSE))

        self.run_plugin_with_args(workflow)

    def test_signing_intent_and_compose_ids_mutex(self, workflow):
        plugin_args = {'compose_ids': [1, 2], 'signing_intent': 'unsigned'}
        self.run_plugin_with_args(workflow, plugin_args,
                                  expect_error='cannot be used at the same time')

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
    def test_odcs_session_creation(self, workflow, plugin_args, expected_kwargs):
        if plugin_args.get('odcs_openidc_secret_path') is True:
            workflow._tmpdir.join('token').write('the-token')
            plugin_args['odcs_openidc_secret_path'] = str(workflow._tmpdir)

        if plugin_args.get('odcs_ssl_secret_path') is True:
            workflow._tmpdir.join('cert').write('the-cert')
            plugin_args['odcs_ssl_secret_path'] = str(workflow._tmpdir)
            expected_kwargs['cert'] = str(workflow._tmpdir.join('cert'))

        (flexmock(ODCSClient)
            .should_receive('__init__')
            .with_args(ODCS_URL, **expected_kwargs))

        self.run_plugin_with_args(workflow, plugin_args)

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
    def test_koji_session_creation(self, workflow, plugin_args, ssl_login):
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

        self.run_plugin_with_args(workflow, plugin_args)

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
    def test_adjust_signing_intent(self, workflow, default_si, config_si, arg_si, parent_si,
                                   expected_si, overridden, use_compose_id):

        mock_reactor_config(workflow._tmpdir, default_si=default_si)
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
            'parent-image-koji-build': parent_build_info,
        }

        plugin_args = {}
        if arg_si:
            plugin_args['signing_intent'] = arg_si
        if arg_compose_ids:
            plugin_args['compose_ids'] = arg_compose_ids

        plugin_result = self.run_plugin_with_args(workflow, plugin_args)
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
    def test_signing_intent_multiple_composes(self, workflow, composes_intent, expected_intent):
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
        plugin_result = self.run_plugin_with_args(workflow, plugin_args)

        assert plugin_result['signing_intent'] == expected_intent
        assert plugin_result['composes'] == composes

    @pytest.mark.parametrize(('config', 'error_message'), (
        (dedent("""\
            compose:
                packages: []
            """), 'cannot be empty'),

        (dedent("""\
            compose:
                modules: []
            """), 'cannot be empty'),

        (dedent("""\
            compose:
                packages:
                - pkg1
                modules:
                - module1
            """), 'cannot contain both'),
    ))
    def test_invalid_compose_request(self, workflow, config, error_message):
        mock_repo_config(workflow._tmpdir, config)
        self.run_plugin_with_args(workflow, expect_error=error_message)

    @pytest.mark.parametrize(('state_name', 'time_to_expire_delta', 'expect_renew'), (
        ('removed', timedelta(), True),
        ('removed', timedelta(hours=-2), True),
        ('done', timedelta(), True),
        # Grace period to avoid timing issues during test runs
        ('done', timedelta(minutes=118), True),
        ('done', timedelta(hours=3), False),
    ))
    def test_renew_compose(self, workflow, state_name, time_to_expire_delta, expect_renew):
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
        plugin_result = self.run_plugin_with_args(workflow, plugin_args)

        if expect_renew:
            assert plugin_result['composes'] == [new_odcs_compose]
        else:
            assert plugin_result['composes'] == [old_odcs_compose]

    def test_inject_yum_repos_from_new_compose(self, workflow):
        self.run_plugin_with_args(workflow)
        assert self.get_override_yum_repourls(workflow) == [ODCS_COMPOSE_REPOFILE]

    def test_inject_yum_repos_from_existing_composes(self, workflow):
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
        self.run_plugin_with_args(workflow, plugin_args)

        assert self.get_override_yum_repourls(workflow) == expected_yum_repourls

    def test_abort_when_odcs_config_missing(self, caplog, workflow):
        # Clear out default reactor config
        mock_reactor_config(workflow._tmpdir, '')
        with caplog.atLevel(logging.INFO):
            self.run_plugin_with_args(workflow)

        msg = 'Aborting plugin execution: ODCS config not found'
        assert msg in (x.message for x in caplog.records())

    def test_abort_when_compose_config_missing(self, caplog, workflow):
        # Clear out default git repo config
        mock_repo_config(workflow._tmpdir, '')
        # Ensure no compose_ids are passed to plugin
        plugin_args = {'compose_ids': tuple()}
        with caplog.atLevel(logging.INFO):
            self.run_plugin_with_args(workflow, plugin_args)

        msg = 'Aborting plugin execution: "compose" config not set and compose_ids not given'
        assert msg in (x.message for x in caplog.records())

    def test_invalid_koji_build_target(self, workflow):
        plugin_args = {
            'koji_hub': KOJI_HUB,
            'koji_target': 'spam',
        }
        expect_error = 'No matching build target found'
        self.run_plugin_with_args(workflow, plugin_args, expect_error=expect_error)

    @pytest.mark.parametrize(('plugin_args', 'msg'), (
        ({'signing_intent': 'spam'},
         'Autorebuild detected: Ignoring signing_intent plugin parameter'),

        ({'compose_ids': [1, 2, 3]},
         'Autorebuild detected: Ignoring compose_ids plugin parameter'),
    ))
    def test_parameters_ignored_for_autorebuild(self, caplog, workflow, plugin_args, msg):
        flexmock(pre_check_and_set_rebuild).should_receive('is_rebuild').and_return(True)
        with caplog.atLevel(logging.INFO):
            self.run_plugin_with_args(workflow, plugin_args)

        assert msg in (x.message for x in caplog.records())

    def run_plugin_with_args(self, workflow, plugin_args=None, expect_result=None,
                             expect_error=None):
        plugin_args = plugin_args or {}
        plugin_args.setdefault('odcs_url', ODCS_URL)
        plugin_args.setdefault('koji_target', KOJI_TARGET_NAME)
        plugin_args.setdefault('koji_hub', KOJI_HUB)

        runner = PreBuildPluginsRunner(
            workflow.builder.tasker,
            workflow,
            [
                {'name': ReactorConfigPlugin.key, 'args': {'config_path': str(workflow._tmpdir)}},
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
            assert len(self.get_override_yum_repourls(workflow)) > 0
            assert set(results.keys()) == set(['signing_intent', 'signing_intent_overridden',
                                               'composes'])
        else:
            assert self.get_override_yum_repourls(workflow) is None
            assert results is None
        return results

    def get_override_yum_repourls(self, workflow):
        return (workflow.plugin_workspace
                .get(OrchestrateBuildPlugin.key, {})
                .get(WORKSPACE_KEY_OVERRIDE_KWARGS, {})
                .get('yum_repourls'))
