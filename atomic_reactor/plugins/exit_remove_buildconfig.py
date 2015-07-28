"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import json
import os

from osbs.api import OSBS
from osbs.conf import Configuration

from atomic_reactor.plugin import ExitPlugin


class RemoveBuildConfigPlugin(ExitPlugin):
    """
    Remove BuildConfig that triggered this build on failure.

    If this OpenShift Build has failed, remove the BuildConfig for it.

    Details:

    The Build hasn't finished yet, of course, but by this point we can
    find out whether it will be marked as failed by checking the state
    of the workflow.
    """

    key = "remove_buildconfig"
    can_fail = False

    def __init__(self, tasker, workflow, url, verify_ssl=True, use_auth=True):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :param url: str, URL to OSv3 instance
        :param verify_ssl: bool, verify SSL certificate?
        :param use_auth: bool, initiate authentication with openshift?
        """
        # call parent constructor
        super(RemoveBuildConfigPlugin, self).__init__(tasker, workflow)
        self.url = url
        self.verify_ssl = verify_ssl
        self.use_auth = use_auth

    def run(self):
        if not self.workflow.build_process_failed:
            # Nothing to do
            return

        try:
            build_json = json.loads(os.environ["BUILD"])
        except KeyError:
            self.log.error("No $BUILD env variable. "
                           "Probably not running in build container.")
            raise

        metadata = build_json.get("metadata", {})
        kwargs = {}
        if 'namespace' in metadata:
            kwargs['namespace'] = metadata['namespace']

        labels = metadata.get("labels", {})
        try:
            my_buildconfig = labels["buildconfig"]
        except KeyError:
            self.log.error("No BuildConfig for this Build")
            raise

        # initial setup will use host based auth: apache will be set
        # to accept everything from specific IP and will set specific
        # X-Remote-User for such requests
        osbs_conf = Configuration(conf_file=None, openshift_uri=self.url,
                                  use_auth=self.use_auth,
                                  verify_ssl=self.verify_ssl)
        osbs = OSBS(osbs_conf, osbs_conf)

        self.log.debug("Deleting my BuildConfig (%s)", my_buildconfig)
        osbs.delete_buildconfig(my_buildconfig, **kwargs)
