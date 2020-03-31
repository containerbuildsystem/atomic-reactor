"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import os
import json
import koji

from atomic_reactor.plugin import PreBuildPlugin, BuildCanceledException
from atomic_reactor.plugins.pre_reactor_config import (get_koji_session, get_koji, NO_FALLBACK,
                                                       get_openshift_session)
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild
from atomic_reactor.constants import PLUGIN_KOJI_DELEGATE_KEY
from atomic_reactor.util import get_build_json


class KojiDelegatePlugin(PreBuildPlugin):
    """
    When autorebuild is enabled, delegate feature is enabled and
    triggered_after_koji_task is not provided,
    will submit new koji task for autorebuild, and cancel current build.
    In all other cases plugin won't do anything
    """

    key = PLUGIN_KOJI_DELEGATE_KEY
    is_allowed_to_fail = False  # We really want to stop the process

    def __init__(self, tasker, workflow, triggered_after_koji_task=None):
        """
        constructor

        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param triggered_after_koji_task: int, original koji task for autorebuild,
            provided only when this plugin creates new koji task for autorebuild
        """
        # call parent constructor
        super(KojiDelegatePlugin, self).__init__(tasker, workflow)

        koji_setting = get_koji(self.workflow, NO_FALLBACK)
        self.delegate_enabled = koji_setting.get('delegate_task', True)
        self.task_priority = koji_setting.get('delegated_task_priority', None)
        self.triggered_after_koji_task = triggered_after_koji_task
        self.metadata = get_build_json().get("metadata", {})
        self.kojisession = get_koji_session(self.workflow, NO_FALLBACK)
        self.osbs = None

    def cancel_build(self):
        """
        cancel current build
        """
        build_name = self.metadata.get("name")
        if build_name:
            self.osbs.cancel_build(build_name)

    def delegate_task(self):
        """
        create new koji task for autorebuild
        """
        user_params = os.environ['USER_PARAMS']
        user_data = json.loads(user_params)

        git_uri = user_data.get('git_uri')
        git_ref = user_data.get('git_ref')
        source = "%s#%s" % (git_uri, git_ref)
        container_target = user_data.get('koji_target')

        koji_task_id = self.metadata.get('labels', {}).get('original-koji-task-id')
        if not koji_task_id:
            koji_task_id = self.metadata.get('labels', {}).get('koji-task-id')
            if not koji_task_id:
                koji_task_id = 0

        task_opts = {}
        for key in ('yum_repourls', 'git_branch', 'signing_intent', 'compose_ids', 'flatpak'):
            if key in user_data:
                if user_data[key]:
                    task_opts[key] = user_data[key]
        task_opts['triggered_after_koji_task'] = int(koji_task_id)

        task_id = self.kojisession.buildContainer(source, container_target, task_opts,
                                                  priority=self.task_priority)

        self.log.info('Created intermediate task: %s', task_id)

    def run(self):
        """
        run the plugin
        """
        if self.delegate_enabled:
            # will be used in koji_import
            self.workflow.triggered_after_koji_task = self.triggered_after_koji_task

        task_running = False
        koji_task_id = self.metadata.get('labels', {}).get('koji-task-id')
        if koji_task_id:
            task_info = self.kojisession.getTaskInfo(koji_task_id, request=True)
            if task_info:
                task_running = koji.TASK_STATES[task_info['state']] == 'OPEN'
            else:
                self.log.warning("koji-task-id label on build, doesn't exist in koji")
        else:
            self.log.warning("koji-task-id label doesn't exist on build")

        # we don't want to plugin continue when:
        # delegate_task isn't enabled
        # build isn't autorebuild
        # triggered_after_koji_task was provided, but task is running,
        # reason for this is, when we once enable delegating, after first autorebuild
        # buildConfig will already have triggered_after_koji_task in user_params
        # so when koji-task-id for build is running task, that means it is that new
        # already delegated task
        if not self.delegate_enabled:
            self.log.info("delegate_task not enabled, skipping plugin")
            return
        elif not is_rebuild(self.workflow):
            self.log.info("not autorebuild, skipping plugin")
            return
        elif (self.triggered_after_koji_task and task_running):
            self.log.info("koji task already delegated, skipping plugin")
            return

        self.osbs = get_openshift_session(self.workflow, NO_FALLBACK)

        self.delegate_task()

        # we will remove all exit plugins, as we don't want any of them running,
        # mainly sendmail
        self.workflow.exit_plugins_conf = []
        # we will cancel build and raise exception,
        # without canceling build build would end up as failed build, and we don't want
        # to have this build as failed but cancelled so it doesn't inerfere with real failed builds
        self.cancel_build()
        self.log.info('Build was delegated, will cancel itself')
        raise BuildCanceledException("Build was canceled")
