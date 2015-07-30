"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import json
import os

from atomic_reactor.plugin import PreBuildPlugin


def is_rebuild(workflow):
    return (CheckRebuildPlugin.key in workflow.prebuild_results and
            workflow.prebuild_results[CheckRebuildPlugin.key])


class CheckRebuildPlugin(PreBuildPlugin):
    """
    Determine whether this is an automated rebuild

    If this is the first build, there will be a label set in the
    metadata to say so. The OSBS client sets this label when it
    creates the BuildConfig, but removes it after instantiating a
    Build.

    If that label is not present, this must be an automated rebuild.

    Example configuration:

    {
      "name": "check_rebuild",
      "args": {
        "key": "client",
        "value": "osbs"
      }
    }
    """

    key = "check_rebuild"
    can_fail = False  # We really want to stop the process

    def __init__(self, tasker, workflow, key, value):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param key: str, key of label used to indicate first build
        :param value: str, value of label used to indicate first build
        """
        # call parent constructor
        super(CheckRebuildPlugin, self).__init__(tasker, workflow)
        self.label_key = key
        self.label_value = value

    def run(self):
        """
        run the plugin
        """

        try:
            build_json = json.loads(os.environ["BUILD"])
        except KeyError:
            self.log.error("No $BUILD env variable. Probably not running in build container")
            raise

        metadata = build_json.get("metadata", {})
        if self.label_key in metadata:
            if metadata[self.label_key] == self.label_value:
                self.log.info("This is not a rebuild")
                return False

        self.log.info("This is a rebuild")
        return True

