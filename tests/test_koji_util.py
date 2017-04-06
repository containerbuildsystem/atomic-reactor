"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import, print_function, unicode_literals

try:
    import koji
except ImportError:
    import inspect
    import os
    import sys

    # Find our mocked koji module
    import tests.koji as koji
    mock_koji_path = os.path.dirname(inspect.getfile(koji.ClientSession))
    if mock_koji_path not in sys.path:
        sys.path.append(os.path.dirname(mock_koji_path))

    # Now load it properly, the same way the module we're testing will
    del koji
    import koji

from atomic_reactor.koji_util import (koji_login, create_koji_session,
                                      TaskWatcher, tag_koji_build)
from atomic_reactor import koji_util
from atomic_reactor.plugin import BuildCanceledException
import flexmock
import pytest


class TestKojiLogin(object):
    @pytest.mark.parametrize('proxyuser', [None, 'proxy'])
    def test_koji_login_krb_keyring(self, proxyuser):
        session = flexmock()
        expectation = session.should_receive('krb_login').once().and_return(True)
        kwargs = {}
        if proxyuser is not None:
            expectation.with_args(proxyuser=proxyuser)
            kwargs['proxyuser'] = proxyuser
        else:
            expectation.with_args()

        koji_login(session, **kwargs)

    @pytest.mark.parametrize('proxyuser', [None, 'proxy'])
    def test_koji_login_krb_keytab(self, proxyuser):
        session = flexmock()
        expectation = session.should_receive('krb_login').once().and_return(True)
        principal = 'user'
        keytab = '/keytab'
        call_kwargs = {
            'krb_principal': principal,
            'krb_keytab': keytab,
        }
        exp_kwargs = {
            'principal': principal,
            'keytab': keytab,
        }
        if proxyuser is not None:
            call_kwargs['proxyuser'] = proxyuser
            exp_kwargs['proxyuser'] = proxyuser

        expectation.with_args(**exp_kwargs)
        koji_login(session, **call_kwargs)

    @pytest.mark.parametrize('proxyuser', [None, 'proxy'])
    def test_koji_login_ssl(self, proxyuser):
        session = flexmock()
        expectation = session.should_receive('ssl_login').once().and_return(True)
        call_kwargs = {
            'ssl_certs_dir': '/certs',
        }
        exp_kwargs = {}
        if proxyuser:
            call_kwargs['proxyuser'] = proxyuser
            exp_kwargs['proxyuser'] = proxyuser

        expectation.with_args('/certs/cert', '/certs/ca', '/certs/serverca',
                              **exp_kwargs)
        koji_login(session, **call_kwargs)


class TestCreateKojiSession(object):
    def test_create_simple_session(self):
        url = 'https://koji-hub-url.com'
        session = flexmock()

        (flexmock(koji_util.koji).should_receive('ClientSession').with_args(
            url, opts={'krb_rdns': False}).and_return(session)
        )
        assert create_koji_session(url) == session

    def test_create_authenticated_session(self):
        url = 'https://koji-hub-url.com'
        session = flexmock()
        session.should_receive('krb_login').once().and_return(True)

        (flexmock(koji_util.koji).should_receive('ClientSession').with_args(
            url, opts={'krb_rdns': False}).and_return(session)
        )
        assert create_koji_session(url, {}) == session


class TestStreamTaskOutput(object):
    def test_output_as_generator(self):
        contents = 'this is the simulated file contents'

        session = flexmock()
        expectation = session.should_receive('downloadTaskOutput')

        for chunk in contents:
            expectation = expectation.and_return(chunk)
        # Empty content to simulate end of stream.
        expectation.and_return('')

        streamer = koji_util.stream_task_output(session, 123, 'file.ext')
        assert ''.join(list(streamer)) == contents


class TestTaskWatcher(object):
    @pytest.mark.parametrize(('finished', 'info', 'exp_state', 'exp_failed'), [
        ([False, False, True],
         {'state': koji.TASK_STATES['CANCELED']},
         'CANCELED', True),

        ([False, True],
         {'state': koji.TASK_STATES['FAILED']},
         'FAILED', True),

        ([True],
         {'state': koji.TASK_STATES['CLOSED']},
         'CLOSED', False),
    ])
    def test_wait(self, finished, info, exp_state, exp_failed):
        session = flexmock()
        task_id = 1234
        task_finished = (session.should_receive('taskFinished')
                         .with_args(task_id))
        for finished_value in finished:
            task_finished = task_finished.and_return(finished_value)

        (session.should_receive('getTaskInfo')
            .with_args(task_id, request=True)
            .once()
            .and_return(info))

        task = TaskWatcher(session, task_id, poll_interval=0)
        assert task.wait() == exp_state
        assert task.failed() == exp_failed

    def test_cancel(self):
        session = flexmock()
        task_id = 1234
        (session
            .should_receive('taskFinished')
            .with_args(task_id)
            .and_raise(BuildCanceledException))

        task = TaskWatcher(session, task_id, poll_interval=0)
        with pytest.raises(BuildCanceledException):
            task.wait()

        assert task.failed()


class TestTagKojiBuild(object):
    @pytest.mark.parametrize(('task_state', 'failure'), (
        ('CLOSED', False),
        ('CANCELED', True),
        ('FAILED', True),
    ))
    def test_tagging(self, task_state, failure):
        session = flexmock()
        task_id = 9876
        build_id = 1234
        target_name = 'target'
        tag_name = 'images-candidate'
        target_info = {'dest_tag_name': tag_name}
        task_info = {'state': koji.TASK_STATES[task_state]}

        (session
            .should_receive('getBuildTarget')
            .with_args(target_name)
            .and_return(target_info))
        (session
            .should_receive('tagBuild')
            .with_args(tag_name, build_id)
            .and_return(task_id))
        (session
            .should_receive('taskFinished')
            .with_args(task_id)
            .and_return(True))
        (session
            .should_receive('getTaskInfo')
            .with_args(task_id, request=True)
            .and_return(task_info))

        if failure:
            with pytest.raises(RuntimeError):
                tag_koji_build(session, build_id, target_name)
        else:
            build_tag = tag_koji_build(session, build_id, target_name)
            assert build_tag == tag_name
