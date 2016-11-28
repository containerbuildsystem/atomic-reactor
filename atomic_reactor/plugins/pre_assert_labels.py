"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Make sure Dockerfile contains Name/Version/Release
(or others if specified) labels.
"""

from __future__ import unicode_literals

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import df_parser

class AssertLabelsPlugin(PreBuildPlugin):
    key = "assert_labels"
    is_allowed_to_fail = False  # We really want to stop the process

    def __init__(self, tasker, workflow, required_labels=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param required_labels: list of labels that will be checked
        """
        # call parent constructor
        super(AssertLabelsPlugin, self).__init__(tasker, workflow)

        self.required_labels = required_labels or ['name', 'version', 'release']

    def run(self):
        """
        run the plugin
        """
        labels = df_parser(self.workflow.builder.df_path, workflow=self.workflow).labels
        for label in self.required_labels:
            if labels.get(label) is None:
                msg = "Dockerfile is missing '{0}' label.".format(label)
                self.log.error(msg)
                raise AssertionError(msg)
