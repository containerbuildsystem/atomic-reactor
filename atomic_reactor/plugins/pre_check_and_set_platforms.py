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
                                 get_orchestrator_platforms, map_to_user_params)
from atomic_reactor.constants import PLUGIN_CHECK_AND_SET_PLATFORMS_KEY
from atomic_reactor.config import get_koji_session


class CheckAndSetPlatformsPlugin(PreBuildPlugin):
    key = PLUGIN_CHECK_AND_SET_PLATFORMS_KEY
    is_allowed_to_fail = False

    args_from_user_params = map_to_user_params("koji_target")

    def __init__(self, workflow, koji_target=None):

        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :param koji_target: str, Koji build target name
        """
        # call parent constructor
        super(CheckAndSetPlatformsPlugin, self).__init__(workflow)
        self.koji_target = koji_target

    def run(self):
        """
        run the plugin
        """
        if self.koji_target:
            koji_session = get_koji_session(self.workflow.conf)
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
            self.log.info("Koji platforms are %s", sorted(platforms))

            if is_scratch_build(self.workflow) or is_isolated_build(self.workflow):
                override_platforms = get_orchestrator_platforms(self.workflow)
                if override_platforms and set(override_platforms) != set(platforms):
                    sort_platforms = sorted(override_platforms)
                    self.log.info("Received user specified platforms %s", sort_platforms)
                    self.log.info("Using them instead of koji platforms")
                    # platforms from user params do not match platforms from koji target
                    # that almost certainly means they were overridden and should be used
                    return set(override_platforms)
        else:
            platforms = get_orchestrator_platforms(self.workflow)
            user_platforms = sorted(platforms) if platforms else None
            self.log.info("No koji platforms. User specified platforms are %s", user_platforms)

        if not platforms:
            raise RuntimeError("Cannot determine platforms; no koji target or platform list")

        # Filter platforms based on clusters
        enabled_platforms = []
        defined_but_disabled = []
        for p in platforms:
            if self.workflow.conf.get_enabled_clusters_for_platform(p):
                enabled_platforms.append(p)
            elif self.workflow.conf.cluster_defined_for_platform(p):
                defined_but_disabled.append(p)
            else:
                self.log.warning("No cluster found for platform '%s' in "
                                 "reactor config map, skipping", p)

        if defined_but_disabled:
            msg = 'Platforms specified in config map, but have all clusters disabled' \
                  ' {}'.format(defined_but_disabled)
            raise RuntimeError(msg)

        final_platforms = get_platforms_in_limits(self.workflow, enabled_platforms)

        self.log.info("platforms in limits : %s", final_platforms)

        if not final_platforms:
            self.log.error("platforms in limits are empty")
            raise RuntimeError("No platforms to build for")

        return final_platforms
