"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import koji

from atomic_reactor.plugin import PreBuildPlugin, BuildCanceledException
from atomic_reactor.plugins.pre_reactor_config import (get_koji_session, get_koji, NO_FALLBACK,
                                                       get_openshift_session)
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild
from atomic_reactor.constants import PLUGIN_KOJI_DELEGATE_KEY
from atomic_reactor.util import get_build_json


class KojiDelegatePlugin(PreBuildPlugin):
    """
    Delegate the build to a new koji task

    When the autorebuild and koji_delegate features are enabled, and a
    triggered_after_koji_task param is not provided, this plugin will submit a
    new koji task for the autorebuild. The current build will be cancelled.  In
    all other cases, this plugin won't do anything.
    """

    key = PLUGIN_KOJI_DELEGATE_KEY
    is_allowed_to_fail = False  # We really want to stop the process

    def __init__(self, tasker, workflow, triggered_after_koji_task=None):
        """
        constructor

        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param triggered_after_koji_task: int, original koji task for the autorebuild,
            provided only when this plugin creates a new koji task for the autorebuild
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
        cancel the current build
        """
        build_name = self.metadata.get("name")
        if build_name:
            self.osbs.cancel_build(build_name)

    def delegate_task(self):
        """
        create a new koji task to perform the autorebuild
        """
        git_uri = self.workflow.user_params.get('git_uri')
        git_ref = self.workflow.user_params.get('git_ref')
        source = "%s#%s" % (git_uri, git_ref)
        container_target = self.workflow.user_params.get('koji_target')

        koji_task_id = self.metadata.get('labels', {}).get('original-koji-task-id')
        if not koji_task_id:
            koji_task_id = self.metadata.get('labels', {}).get('koji-task-id')
            if not koji_task_id:
                koji_task_id = 0

        task_opts = {}
        for key in ('yum_repourls', 'git_branch', 'signing_intent', 'compose_ids', 'flatpak'):
            if key in self.workflow.user_params:
                if self.workflow.user_params[key]:
                    task_opts[key] = self.workflow.user_params[key]
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

        if not self.delegate_enabled:
            self.log.info("delegate_task not enabled, skipping plugin")
            return
        elif not is_rebuild(self.workflow):
            self.log.info("not autorebuild, skipping plugin")
            return
        elif (self.triggered_after_koji_task and task_running):
            # The buildConfig will already have triggered_after_koji_task in user_params
            # after the first autorebuild performed with the delegating feature enabled.
            # If koji-task-id for the build is a running task,
            # it means it is a new, already delegated task
            self.log.info("koji task already delegated, skipping plugin")
            return

        self.osbs = get_openshift_session(self.workflow, NO_FALLBACK)

        # Do not run exit plugins. Especially sendmail
        self.workflow.exit_plugins_conf = []

        if self.workflow.cancel_isolated_autorebuild:  # this is set by the koji_parent plugin
            self.log.info("ignoring isolated build for autorebuild, the build will be cancelled")
            self.cancel_build()
            raise BuildCanceledException("Build was canceled")

        self.delegate_task()

        # We cancel the build so it does not inerfere with real failed builds
        self.cancel_build()
        self.log.info('Build was delegated, the build will be cancelled')
        raise BuildCanceledException("Build was canceled")
