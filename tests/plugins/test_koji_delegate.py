"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import os
from copy import deepcopy
from flexmock import flexmock
import pytest
import json
import koji as koji

from atomic_reactor.plugin import BuildCanceledException
from atomic_reactor.plugins.pre_koji_delegate import KojiDelegatePlugin
from atomic_reactor.plugins.pre_check_and_set_rebuild import CheckAndSetRebuildPlugin
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from osbs.api import OSBS


class MockSource(object):
    def __init__(self, tmpdir):
        self.dockerfile_path = str(tmpdir.join('Dockerfile'))
        self.path = str(tmpdir)
        self.commit_id = None
        self.config = flexmock(autorebuild=dict())


class TestKojiDelegate(object):
    def prepare(self,
                tmpdir,
                is_auto=False,
                triggered_after_koji_task=None,
                delegate_task=False,
                delegated_priority=None):

        workflow = flexmock()
        setattr(workflow, 'builder', flexmock())
        setattr(workflow, 'plugin_workspace', {})
        setattr(workflow, 'reserved_build_id', None)
        setattr(workflow, 'reserved_token ', None)
        setattr(workflow, 'triggered_after_koji_task', None)
        setattr(workflow, 'source', MockSource(tmpdir))
        setattr(workflow, 'prebuild_results', {CheckAndSetRebuildPlugin.key: is_auto})

        df = tmpdir.join('Dockerfile')
        df.write('FROM base\n')
        setattr(workflow.builder, 'df_path', str(df))

        kwargs = {
            'tasker': None,
            'workflow': workflow,
        }
        if triggered_after_koji_task:
            kwargs['triggered_after_koji_task'] = triggered_after_koji_task
        koji_map = {
            'hub_url': '',
            'root_url': '',
            'auth': {},
            'reserve_build': False,
            'delegate_task': delegate_task,
        }
        openshift_map = {
            'url': '',
            'insecure': False,
            'auth': {'enable': True}
        }
        if delegated_priority:
            koji_map['delegated_task_priority'] = delegated_priority

        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig({'version': 1, 'koji': koji_map, 'openshift': openshift_map})

        plugin = KojiDelegatePlugin(**kwargs)
        return plugin

    @pytest.mark.parametrize(('delegate_task', 'is_auto', 'triggered_task', 'task_open',
                              'koji_task_id', 'task_exists'), [
        (False, False, None, False, True, True),
        (False, True, None, False, True, True),
        (False, False, None, True, True, True),
        (False, True, None, True, True, True),
        (False, False, 12345, False, True, True),
        (False, False, 12345, False, False, False),
        (False, False, 12345, False, True, False),
        (False, True, 12345, False, True, True),
        (False, True, 12345, False, False, False),
        (False, True, 12345, False, True, False),
        (False, False, 12345, True, True, True),
        (False, True, 12345, True, True, True),
        (True, False, None, False, True, True),
        (True, False, None, True, True, True),
        (True, False, 12345, False, True, True),
        (True, False, 12345, False, False, False),
        (True, False, 12345, False, True, False),
        (True, False, 12345, True, True, True),
        (True, True, 12345, True, True, True),
    ])
    def test_skip_delegate_build(self, tmpdir, caplog, delegate_task, is_auto, triggered_task,
                                 task_open, koji_task_id, task_exists):
        class MockedClientSession(object):
            def __init__(self, hub, opts=None):
                pass

            def getBuild(self, build_info):
                return None

            def krb_login(self, *args, **kwargs):
                return True

            def getTaskInfo(self, task_id, request=False):
                if not task_exists:
                    return None
                if task_open:
                    return {'state': koji.TASK_STATES['OPEN']}
                else:
                    return {'state': koji.TASK_STATES['CLOSED']}

        session = MockedClientSession('')
        flexmock(koji, ClientSession=session)

        new_environ = deepcopy(os.environ)
        build_json = {
            "metadata": {
                "name": "auto-123456",
                "labels": {}
            }
        }
        if koji_task_id:
            build_json['metadata']['labels']['koji-task-id'] = 12345
        new_environ["BUILD"] = json.dumps(build_json)

        flexmock(os)
        os.should_receive("environ").and_return(new_environ)  # pylint: disable=no-member

        plugin = self.prepare(tmpdir, is_auto=is_auto, delegate_task=delegate_task,
                              triggered_after_koji_task=triggered_task)
        plugin.run()
        if delegate_task:
            assert plugin.workflow.triggered_after_koji_task == triggered_task
        else:
            assert plugin.workflow.triggered_after_koji_task is None

        if not delegate_task:
            assert "delegate_task not enabled, skipping plugin" in caplog.text
        elif not is_auto:
            assert "not autorebuild, skipping plugin" in caplog.text
        elif triggered_task and task_open:
            assert "koji task already delegated, skipping plugin" in caplog.text

        if not koji_task_id:
            assert "koji-task-id label doesn't exist on build" in caplog.text
        elif not task_exists:
            assert "koji-task-id label on build, doesn't exist in koji" in caplog.text

    @pytest.mark.parametrize('user_params', [
        {'git_ref': 'test_ref',
         'git_uri': 'test_uri',
         'git_branch': 'test_branch'},

        {'git_ref': 'test_ref',
         'git_uri': 'test_uri',
         'git_branch': 'test_branch',
         'yum_repourls': ['yum_url1', 'yum_url2'],
         'signing_intent': 'test_intent',
         'compose_ids': [1, 2, 3]},
    ])
    @pytest.mark.parametrize(('koji_task_id', 'original_koji_task_id'), [
        (12345, None),
        (12345, 67890),
        (None, 67890),
        (None, None),
    ])
    @pytest.mark.parametrize(('triggered_task', 'task_open', 'task_priority'), [
        (12345, False, None),
        (None, True, 30),
        (None, False, 60),
    ])
    def test_delegate_build(self, tmpdir, caplog, user_params, koji_task_id, original_koji_task_id,
                            triggered_task, task_open, task_priority):
        class MockedClientSession(object):
            def __init__(self, hub, opts=None):
                pass

            def getBuild(self, build_info):
                return None

            def krb_login(self, *args, **kwargs):
                return True

            def getTaskInfo(self, task_id, request=False):
                if task_open:
                    return {'state': koji.TASK_STATES['OPEN']}
                else:
                    return {'state': koji.TASK_STATES['CLOSED']}

            def buildContainer(self, source, container_target,  task_opts, priority=None):
                expect_source = "%s#%s" % (user_params.get('git_uri'), user_params.get('git_ref'))
                assert source == expect_source
                assert container_target == user_params.get('koji_target')
                assert priority == task_priority

                expect_opts = {
                    'git_branch': user_params.get('git_branch'),
                    'triggered_after_koji_task': original_koji_task_id,
                }
                if user_params.get('yum_repourls'):
                    expect_opts['yum_repourls'] = user_params.get('yum_repourls')
                if user_params.get('signing_intent'):
                    expect_opts['signing_intent'] = user_params.get('signing_intent')
                if user_params.get('compose_ids'):
                    expect_opts['compose_ids'] = user_params.get('compose_ids')
                if not expect_opts['triggered_after_koji_task']:
                    expect_opts['triggered_after_koji_task'] = koji_task_id or 0

                assert expect_opts == task_opts
                return 987654321

        session = MockedClientSession('')
        flexmock(koji, ClientSession=session)

        build_name = "auto-123456"
        new_environ = deepcopy(os.environ)
        build_json = {
            "metadata": {
                "name": build_name,
                "labels": {"koji-task-id": koji_task_id}
            }
        }
        if original_koji_task_id:
            build_json['metadata']['labels']['original-koji-task-id'] = original_koji_task_id
        new_environ["BUILD"] = json.dumps(build_json)
        new_environ["USER_PARAMS"] = json.dumps(user_params)

        flexmock(os)
        os.should_receive("environ").and_return(new_environ)  # pylint: disable=no-member
        flexmock(OSBS).should_receive('cancel_build').with_args(build_name).once()

        plugin = self.prepare(tmpdir, is_auto=True, delegate_task=True,
                              delegated_priority=task_priority,
                              triggered_after_koji_task=triggered_task)

        with pytest.raises(BuildCanceledException):
            plugin.run()

        assert 'Created intermediate task: 987654321' in caplog.text
        assert 'Build was delegated, will cancel itself' in caplog.text
