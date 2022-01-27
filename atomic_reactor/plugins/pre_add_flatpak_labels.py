"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pre build plugin which adds additional labels to the Dockerfile automatically
created for a flatpak, based on the flatpak: labels key in container.yaml.
"""

from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import label_to_string, is_flatpak_build


class AddFlatpakLabelsPlugin(PreBuildPlugin):
    key = "add_flatpak_labels"
    is_allowed_to_fail = False

    def __init__(self, workflow):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        """
        # call parent constructor
        super(AddFlatpakLabelsPlugin, self).__init__(workflow)

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

        labels_str = " ".join(label_to_string(k, v) for k, v in sorted(labels.items()))
        label_line = f"\nLABEL {labels_str}\n"

        def add_labels_to_df(build_dir: BuildDir) -> None:
            dockerfile = build_dir.dockerfile
            # put labels at the end of dockerfile (since they change metadata and do not interact
            # with FS, this should cause no harm)
            dockerfile.lines = dockerfile.lines + [label_line]

        self.workflow.build_dir.for_each_platform(add_labels_to_df)
