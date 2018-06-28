"""
Copyright (c) 2018 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Query the koji build target, if any, to find the enabled architectures. Remove any excluded
architectures, and return the resulting list.

build_orchestrate_build will prefer this list of architectures over the platforms supplied by
USER_PARAMS, which is necessary to allow autobuilds to build on the correct architectures
when koji build tags change.
"""

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import (get_platforms_in_limits, is_scratch_build, is_isolated_build,
                                 get_orchestrator_platforms)
from atomic_reactor.plugins.pre_reactor_config import get_koji_session, NO_FALLBACK
from atomic_reactor.constants import PLUGIN_CHECK_AND_SET_PLATFORMS_KEY


class CheckAndSetPlatformsPlugin(PreBuildPlugin):
    key = PLUGIN_CHECK_AND_SET_PLATFORMS_KEY
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, koji_target):

        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param koji_target: str, Koji build target name
        """
        # call parent constructor
        super(CheckAndSetPlatformsPlugin, self).__init__(tasker, workflow)
        self.koji_target = koji_target

    def run(self):
        """
        run the plugin
        """
        koji_session = get_koji_session(self.workflow, NO_FALLBACK)
        self.log.info("Checking koji target for platforms")
        event_id = koji_session.getLastEvent()['id']
        target_info = koji_session.getBuildTarget(self.koji_target, event=event_id)
        build_tag = target_info['build_tag']
        koji_build_conf = koji_session.getBuildConfig(build_tag, event=event_id)
        koji_platforms = koji_build_conf['arches']
        if not koji_platforms:
            self.log.info("No platforms found in koji target")
            return None
        platforms = koji_platforms.split()

        if is_scratch_build() or is_isolated_build():
            override_platforms = get_orchestrator_platforms(self.workflow)
            if override_platforms and set(override_platforms) != koji_platforms:
                # platforms from user params do not match platforms from koji target
                # that almost certainly means they were overridden and should be used
                return set(override_platforms)

        return get_platforms_in_limits(self.workflow, platforms)
