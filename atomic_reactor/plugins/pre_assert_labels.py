"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Make sure Dockerfile contains Name/Version/Release
(or others if specified) labels.
"""

from __future__ import unicode_literals

from dockerfile_parse import DockerfileParser
from atomic_reactor.plugin import PreBuildPlugin

class AssertLabelsPlugin(PreBuildPlugin):
    key = "assert_labels"
    is_allowed_to_fail = False  # We really want to stop the process

    def __init__(self, tasker, workflow, required_labels=None, deprecated_labels=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param required_labels: list of labels that will be checked
        :param deprecated_labels: dictionary of deprecated labels where keys are the old labels and
                                  values are the recommended new labels
        """
        # call parent constructor
        super(AssertLabelsPlugin, self).__init__(tasker, workflow)

        self.required_labels = required_labels or ['Name', 'Version', 'Release']
        self.deprecated_labels = deprecated_labels or {}

    def run(self):
        """
        run the plugin
        """
        labels = DockerfileParser(self.workflow.builder.df_path).labels
        used_deprecated_labels = set()

        for old, new in self.deprecated_labels.items():
            if labels.get(old) is not None:
                self.log.warning("Label %r is deprecated! Please use %r as a replacement.",
                                 old, new)
                used_deprecated_labels.add(new)

        for label in self.required_labels:
            if labels.get(label) is None and \
                    label not in used_deprecated_labels:
                msg = "Dockerfile is missing '{0}' label.".format(label)
                self.log.error(msg)
                raise AssertionError(msg)
