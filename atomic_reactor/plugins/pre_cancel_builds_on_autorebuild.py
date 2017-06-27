"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from osbs.api import OSBS
from osbs.conf import Configuration
from osbs.exceptions import OsbsResponseException
from atomic_reactor.util import get_build_json
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild


class CancelBuildsOnAutoRebuild(PreBuildPlugin):
    """
    If the current build is rebuild, we need to query OSBS to make sure there
    are no other builds currently running for this container and build target
    """
    key = 'cancel_builds_on_autorebuild'

    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, url, verify_ssl=True, use_auth=True):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param url: str, URL to OSv3 instance
        :param verify_ssl: bool, verify SSL certificate?
        :param use_auth: bool, initiate authentication with OSv3?
        """
        super(CancelBuildsOnAutoRebuild, self).__init__(tasker, workflow)

        self.url = url
        self.verify_ssl = verify_ssl
        self.use_auth = use_auth

    def run(self):

        metadata = get_build_json().get("metadata", {})
        labels = metadata.get("labels", {})
        buildconfig = labels["buildconfig"]

        self.log.info("This is a rebuild? %s", buildconfig)
        if not is_rebuild(self.workflow):
            self.log.info(
                'this is not an autorebuild, %s is doing nothing' % self.key
            )
        else:
            self.log.info(
                'this is an autorebuild, determining if any previous builds need to be cancelled'
            )

            osbs_conf = Configuration(
                conf_file=None,
                openshift_url=self.url,
                use_auth=self.use_auth,
                verify_ssl=self.verify_ssl,
                namespace=metadata.get('namespace', None)
            )

            osbs = OSBS(osbs_conf, osbs_conf)

            try:
                builds = osbs.list_builds(
                    field_selector="buildconfig={}".format(buildconfig)
                )

                for build in builds:
                    if build.is_running():
                        self.log.info(
                            "cancelling build %s in favor of autorebuild",
                            build.build_id
                        )
                        osbs.cancel_build(build.get_build_name())

            except OsbsResponseException as ex:
                self.log.exception("failed to cancel build %s", build.get_build_name())
                raise ex
