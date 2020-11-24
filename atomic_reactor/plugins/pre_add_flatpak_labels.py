"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pre build plugin which adds additional labels to the Dockerfile automatically
created for a flatpak, based on the flatpak: labels key in container.yaml.
"""

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import df_parser, label_to_string, is_flatpak_build


class AddFlatpakLabelsPlugin(PreBuildPlugin):
    key = "add_flatpak_labels"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow):
        """
        constructor

        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        """
        # call parent constructor
        super(AddFlatpakLabelsPlugin, self).__init__(tasker, workflow)

    def run(self):
        """
        run the plugin
        """
        if not is_flatpak_build(self.workflow):
            self.log.info('not flatpak build, skipping plugin')
            return

        flatpak_yaml = self.workflow.source.config.flatpak
        if flatpak_yaml is None:
            return
        labels = flatpak_yaml.get('labels')
        if not labels:
            return

        dockerfile = df_parser(self.workflow.builder.df_path, workflow=self.workflow)
        lines = dockerfile.lines

        # Sort to get repeatable results with Python2
        formatted_labels = []
        for k in sorted(labels):
            formatted_labels.append(label_to_string(k, labels[k]))

        # put labels at the end of dockerfile (since they change metadata and do not interact
        # with FS, this should cause no harm)
        lines.append('\nLABEL ' + " ".join(formatted_labels) + '\n')
        dockerfile.lines = lines
