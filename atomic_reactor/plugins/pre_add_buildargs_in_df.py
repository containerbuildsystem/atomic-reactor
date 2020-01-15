"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Adds all provided buildargs as ARG after each FROM in Dockerfile
"""

from __future__ import absolute_import

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import df_parser


class AddBuildargsPlugin(PreBuildPlugin):
    key = 'add_buildargs_in_dockerfile'
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow):
        """
        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        """
        super(AddBuildargsPlugin, self).__init__(tasker, workflow)

    def run(self):
        """
        Run the plugin.
        """
        if not self.workflow.builder.buildargs:
            self.log.info('No buildargs specified, skipping plugin')
            return

        buildarg_lines = []

        for buildarg in sorted(self.workflow.builder.buildargs.keys()):
            buildarg_lines.append("ARG {}".format(buildarg))

        df = df_parser(self.workflow.builder.df_path, workflow=self.workflow)

        df.add_lines(*buildarg_lines, at_start=True, all_stages=True)
