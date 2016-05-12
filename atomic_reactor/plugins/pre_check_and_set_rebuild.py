"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from osbs.api import OSBS
from osbs.conf import Configuration
from osbs.exceptions import OsbsResponseException
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import get_build_json


def is_rebuild(workflow):
    return (CheckAndSetRebuildPlugin.key in workflow.prebuild_results and
            workflow.prebuild_results[CheckAndSetRebuildPlugin.key])


class CheckAndSetRebuildPlugin(PreBuildPlugin):
    """
    Determine whether this is an automated rebuild

    This plugin checks for a specific label in the OSv3 Build
    metadata. If it exists and has the value specified in the
    configuration, this build is a rebuild. The module-level function
    'is_rebuild()' can be used by other plugins to determine this.

    After checking for the label, it sets the label in the
    metadata, allowing future automated rebuilds to be detected as
    rebuilds.

    Example configuration:

    {
      "name": "check_and_set_rebuild",
      "args": {
        "label_key": "rebuild",
        "label_value": "true",
        "url": "https://localhost:8443/"
      }
    }

    """

    key = "check_and_set_rebuild"
    is_allowed_to_fail = False  # We really want to stop the process

    def __init__(self, tasker, workflow, label_key, label_value,
                 url, verify_ssl=True, use_auth=True):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param label_key: str, key of label used to indicate first build
        :param label_value: str, value of label used to indicate first build
        :param url: str, URL to OSv3 instance
        :param verify_ssl: bool, verify SSL certificate?
        :param use_auth: bool, initiate authentication with OSv3?
        """
        # call parent constructor
        super(CheckAndSetRebuildPlugin, self).__init__(tasker, workflow)
        self.label_key = label_key
        self.label_value = label_value
        self.url = url
        self.verify_ssl = verify_ssl
        self.use_auth = use_auth

    def run(self):
        """
        run the plugin
        """

        metadata = get_build_json().get("metadata", {})
        labels = metadata.get("labels", {})
        buildconfig = labels["buildconfig"]
        is_rebuild = labels.get(self.label_key) == self.label_value
        self.log.info("This is a rebuild? %s", is_rebuild)

        if not is_rebuild:
            # Update the BuildConfig metadata so the next Build
            # instantiated from it is detected as being an automated
            # rebuild

            # FIXME: remove `openshift_uri` once osbs-client is released
            osbs_conf = Configuration(conf_file=None, openshift_uri=self.url,
                                      openshift_url=self.url,
                                      use_auth=self.use_auth,
                                      verify_ssl=self.verify_ssl,
                                      namespace=metadata.get('namespace', None))
            osbs = OSBS(osbs_conf, osbs_conf)
            labels = {self.label_key: self.label_value}
            try:
                osbs.set_labels_on_build_config(buildconfig, labels)
            except OsbsResponseException as ex:
                if ex.status_code == 409:
                    # Someone else was modifying the build
                    # configuration at the same time. Try again.
                    self.log.debug("got status %d, retrying", ex.status_code)
                    osbs.set_labels_on_build_config(buildconfig, labels)
                else:
                    raise

        return is_rebuild
