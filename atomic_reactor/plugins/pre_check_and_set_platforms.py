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
from atomic_reactor.util import get_platforms_in_limits
from atomic_reactor.plugins.pre_reactor_config import (get_koji_session,
                                                       get_platform_to_goarch_mapping,
                                                       NO_FALLBACK)
from atomic_reactor.constants import PLUGIN_CHECK_AND_SET_PLATFORMS_KEY


class MustBuildForAmd64(Exception):
    """ Platforms must include one for GOARCH amd64 """


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
        try:
            self.goarch = get_platform_to_goarch_mapping(workflow)
        except KeyError:
            self.goarch = None

    def validate_platforms(self, platforms):
        """
        Verify that a platform with GOARCH=amd64 is present. If it is not,
        the tag will not be pullable by clients that do not have
        support for the 'manifest list' type.
        """
        if self.goarch is None:
            # Don't perform this check without a real reactor_config_map
            self.log.info("No GOARCH mapping: skipping platform validation")
            return

        for platform in platforms:
            goarch = self.goarch.get(platform, platform)
            if goarch == 'amd64':
                return

        raise MustBuildForAmd64

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
        platforms = get_platforms_in_limits(self.workflow,
                                            koji_platforms.split())
        self.validate_platforms(platforms)
        return platforms
