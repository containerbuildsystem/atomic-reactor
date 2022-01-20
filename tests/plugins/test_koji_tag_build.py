"""
Copyright (c) 2017, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import koji

from atomic_reactor.plugins.exit_koji_tag_build import KojiTagBuildPlugin
from atomic_reactor.plugins.exit_koji_import import KojiImportPlugin
from atomic_reactor.plugin import ExitPluginsRunner, PluginFailedException
from atomic_reactor.inner import BuildResult
from tests.util import add_koji_map_in_workflow

from flexmock import flexmock
import pytest
import time


class MockedClientSession(object):
    TAG_TASK_ID = 1234
    DEST_TAG = 'images-candidate'

    def __init__(self, hub, opts=None, task_states=None):
        self.build_tags = {}
        self.task_states = task_states or ['FREE', 'ASSIGNED', 'CLOSED']

        self.task_states = list(self.task_states)
        self.task_states.reverse()
        self.tag_task_state = self.task_states.pop()

    def krb_login(self, principal=None, keytab=None, proxyuser=None):
        return True

    def ssl_login(self, cert, ca, serverca, proxyuser=None):
        return True

    def getBuildTarget(self, target):
        return {'dest_tag_name': self.DEST_TAG}

    def tagBuild(self, tag, build, force=False, fromtag=None):
        self.build_tags[build] = tag
        return self.TAG_TASK_ID

    def getTaskInfo(self, task_id, request=False):
        assert task_id == self.TAG_TASK_ID

        # For extra code coverage, imagine Koji denies the task ever
        # existed.
        if self.tag_task_state is None:
            return None

        return {'state': koji.TASK_STATES[self.tag_task_state]}

    def taskFinished(self, task_id):
        try:
            self.tag_task_state = self.task_states.pop()
        except IndexError:
            # No more state changes
            pass

        return self.tag_task_state in ['CLOSED', 'FAILED', 'CANCELED', None]


def mock_environment(workflow, session=None, build_process_failed=False,
                     koji_build_id=None, scratch=None):
    if session is None:
        session = MockedClientSession('')

    if scratch is not None:
        workflow.user_params['scratch'] = scratch

    flexmock(koji, ClientSession=lambda hub, opts: session)

    if build_process_failed:
        workflow.data.build_result = BuildResult(fail_reason="not built")
    else:
        workflow.data.build_result = BuildResult(image_id="id1234")

    workflow.data.exit_results[KojiImportPlugin.key] = koji_build_id

    (flexmock(time)
        .should_receive('sleep')
        .and_return(None))


def create_runner(workflow, ssl_certs=False, principal=None,
                  keytab=None, poll_interval=0.01, proxy_user=None,
                  use_args=True, koji_target='koji-target'):
    args = {
        'target': koji_target,
    }

    if poll_interval is not None:
        args['poll_interval'] = poll_interval

    add_koji_map_in_workflow(workflow, hub_url='',
                             ssl_certs_dir='/' if ssl_certs else None,
                             krb_keytab=keytab,
                             krb_principal=principal,
                             proxyuser=proxy_user)

    plugin_conf = {
        'name': KojiTagBuildPlugin.key
    }
    if use_args:
        plugin_conf['args'] = args
    else:
        plugin_conf['args'] = {'target': koji_target}

    runner = ExitPluginsRunner(workflow, [plugin_conf])

    return runner


@pytest.mark.usefixtures('user_params')
class TestKojiPromote(object):
    def test_koji_tag_build_failed_build_process(self, workflow):  # noqa
        session = MockedClientSession('')
        mock_environment(workflow, build_process_failed=True, session=session)
        runner = create_runner(workflow)
        result = runner.run()
        assert result[KojiTagBuildPlugin.key] is None

    @pytest.mark.parametrize('params', [
        {
            'should_raise': False,
            'principal': None,
            'keytab': None,
        },

        {
            'should_raise': False,
            'principal': 'principal@EXAMPLE.COM',
            'keytab': 'FILE:/var/run/secrets/mysecret',
        },

        {
            'should_raise': True,
            'principal': 'principal@EXAMPLE.COM',
            'keytab': None,
        },

        {
            'should_raise': True,
            'principal': None,
            'keytab': 'FILE:/var/run/secrets/mysecret',
        },
    ])
    def test_koji_tag_build_krb_args(self, workflow, params):
        session = MockedClientSession('')
        expectation = flexmock(session).should_receive('krb_login').and_return(True)
        mock_environment(workflow, koji_build_id='98765', session=session)
        runner = create_runner(workflow, principal=params['principal'], keytab=params['keytab'])

        if params['should_raise']:
            expectation.never()
            with pytest.raises(PluginFailedException):
                runner.run()
        else:
            expectation.once()
            runner.run()

    def test_koji_tag_build_krb_fail(self, workflow):  # noqa
        session = MockedClientSession('')
        (flexmock(session)
            .should_receive('krb_login')
            .and_raise(RuntimeError)
            .once())
        mock_environment(workflow, koji_build_id='98765', session=session)
        runner = create_runner(workflow)
        with pytest.raises(PluginFailedException):
            runner.run()

    def test_koji_tag_build_ssl_fail(self, workflow):  # noqa
        session = MockedClientSession('')
        (flexmock(session)
            .should_receive('ssl_login')
            .and_raise(RuntimeError)
            .once())
        mock_environment(workflow, koji_build_id='98765', session=session)
        runner = create_runner(workflow, ssl_certs=True)
        with pytest.raises(PluginFailedException):
            runner.run()

    @pytest.mark.parametrize('task_states', [
        ['FREE', 'ASSIGNED', 'FAILED'],
        ['CANCELED'],
        [None],
    ])
    def test_koji_tag_build_tag_fail(self, workflow, task_states):
        session = MockedClientSession('', task_states=task_states)
        mock_environment(workflow, koji_build_id='98765', session=session)
        runner = create_runner(workflow)
        with pytest.raises(PluginFailedException):
            runner.run()

    def test_koji_tag_build_bad_id(self, workflow):
        mock_environment(workflow, koji_build_id=None)
        runner = create_runner(workflow)
        result = runner.run()
        assert not result[KojiTagBuildPlugin.key]

    def test_koji_tag_build_success(self, workflow):  # noqa
        mock_environment(workflow, koji_build_id='98765')
        runner = create_runner(workflow)
        result = runner.run()
        assert result[KojiTagBuildPlugin.key] == 'images-candidate'

    def test_koji_tag_build_success_no_args(self, workflow):  # noqa
        mock_environment(workflow, koji_build_id='98765')
        runner = create_runner(workflow, use_args=False)
        result = runner.run()
        assert result[KojiTagBuildPlugin.key] == 'images-candidate'

    @pytest.mark.parametrize(('scratch', 'target'), [
        (True, None),
        (True, ''),
        (True, 'some_target'),
        (False, None),
        (False, ''),
    ])
    def test_skip_plugin(self, workflow, caplog, scratch, target):  # noqa
        mock_environment(workflow, koji_build_id='98765', scratch=scratch)
        runner = create_runner(workflow, use_args=False, koji_target=target)
        runner.run()

        if scratch:
            log_msg = 'scratch build, skipping plugin'
        elif not target:
            log_msg = 'no koji target provided, skipping plugin'

        assert log_msg in caplog.text
