"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import os

try:
    import koji
except ImportError:
    import inspect
    import sys

    # Find out mocked koji module
    import tests.koji as koji
    mock_koji_path = os.path.dirname(inspect.getfile(koji.ClientSession))
    if mock_koji_path not in sys.path:
        sys.path.append(os.path.dirname(mock_koji_path))

    # Now load it properly, the same way the plugin will
    del koji
    import koji

from atomic_reactor.core import DockerTasker
from atomic_reactor.plugins.exit_koji_tag_build import KojiTagBuildPlugin
from atomic_reactor.plugins.exit_koji_promote import KojiPromotePlugin
from atomic_reactor.plugin import ExitPluginsRunner, PluginFailedException
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.util import ImageName
from atomic_reactor.build import BuildResult
from tests.constants import SOURCE, MOCK

from flexmock import flexmock
import pytest
from tests.docker_mock import mock_docker


class X(object):
    pass


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


def mock_environment(tmpdir, session=None, build_process_failed=False,
                     koji_build_id=None):
    if session is None:
        session = MockedClientSession('')

    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, 'test-image')
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'image_id', '123456imageid')
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='22'))
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)

    flexmock(koji, ClientSession=lambda hub, opts: session)

    if build_process_failed:
        workflow.build_result = BuildResult(fail_reason="not built")
    else:
        workflow.build_result = BuildResult(image_id="id1234")

    if koji_build_id:
        workflow.exit_results[KojiPromotePlugin.key] = koji_build_id

    return tasker, workflow


def create_runner(tasker, workflow, ssl_certs=False, principal=None,
                  keytab=None, poll_interval=0.01, proxy_user=None):
    args = {
        'kojihub': '',
        'target': 'koji-target',
    }
    if ssl_certs:
        args['koji_ssl_certs'] = '/'

    if principal:
        args['koji_principal'] = principal

    if keytab:
        args['koji_keytab'] = keytab

    if poll_interval is not None:
        args['poll_interval'] = poll_interval

    if proxy_user:
        args['koji_proxy_user'] = proxy_user

    runner = ExitPluginsRunner(tasker, workflow,
                               [
                                   {
                                       'name': KojiTagBuildPlugin.key,
                                       'args': args,
                                   },
                               ])

    return runner


class TestKojiPromote(object):
    def test_koji_tag_build_failed_build_process(self, tmpdir):
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir, build_process_failed=True,
                                            session=session)
        runner = create_runner(tasker, workflow)
        result = runner.run()
        assert result[KojiTagBuildPlugin.key] is None

    def test_koji_tag_build_failed_koji_promote(self, tmpdir):
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir, koji_build_id=None,
                                            session=session)
        runner = create_runner(tasker, workflow)
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
    def test_koji_tag_build_krb_args(self, tmpdir, params):
        session = MockedClientSession('')
        expectation = flexmock(session).should_receive('krb_login').and_return(True)
        tasker, workflow = mock_environment(tmpdir, koji_build_id='98765',
                                            session=session)
        runner = create_runner(tasker, workflow,
                               principal=params['principal'],
                               keytab=params['keytab'])

        if params['should_raise']:
            expectation.never()
            with pytest.raises(PluginFailedException):
                runner.run()
        else:
            expectation.once()
            runner.run()

    def test_koji_tag_build_krb_fail(self, tmpdir):
        session = MockedClientSession('')
        (flexmock(session)
            .should_receive('krb_login')
            .and_raise(RuntimeError)
            .once())
        tasker, workflow = mock_environment(tmpdir, koji_build_id='98765',
                                            session=session)
        runner = create_runner(tasker, workflow)
        with pytest.raises(PluginFailedException):
            runner.run()

    def test_koji_tag_build_ssl_fail(self, tmpdir):
        session = MockedClientSession('')
        (flexmock(session)
            .should_receive('ssl_login')
            .and_raise(RuntimeError)
            .once())
        tasker, workflow = mock_environment(tmpdir, koji_build_id='98765',
                                            session=session)
        runner = create_runner(tasker, workflow, ssl_certs=True)
        with pytest.raises(PluginFailedException):
            runner.run()

    @pytest.mark.parametrize('task_states', [
        ['FREE', 'ASSIGNED', 'FAILED'],
        ['CANCELED'],
        [None],
    ])
    def test_koji_tag_build_tag_fail(self, tmpdir, task_states):
        session = MockedClientSession('', task_states=task_states)
        tasker, workflow = mock_environment(tmpdir, koji_build_id='98765',
                                            session=session)
        runner = create_runner(tasker, workflow)
        with pytest.raises(PluginFailedException):
            runner.run()

    def test_koji_tag_build_success(self, tmpdir):
        tasker, workflow = mock_environment(tmpdir, koji_build_id='98765')
        runner = create_runner(tasker, workflow)
        result = runner.run()
        assert result[KojiTagBuildPlugin.key] == 'images-candidate'
