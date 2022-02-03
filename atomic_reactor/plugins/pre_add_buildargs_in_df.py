"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Adds all provided buildargs as ARG after each FROM in Dockerfile
"""

from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugin import PreBuildPlugin


class AddBuildargsPlugin(PreBuildPlugin):
    key = 'add_buildargs_in_dockerfile'
    is_allowed_to_fail = False

    def __init__(self, workflow):
        """
        :param workflow: DockerBuildWorkflow instance
        """
        super(AddBuildargsPlugin, self).__init__(workflow)

    def add_buildargs(self, build_dir: BuildDir) -> None:
        """Add ARG instructions for each build argument to the Dockerfile."""
        buildarg_lines = [
            f"ARG {buildarg}" for buildarg in sorted(self.workflow.data.buildargs.keys())
        ]
        build_dir.dockerfile.add_lines(*buildarg_lines, at_start=True, all_stages=True)

    def run(self):
        """Run the plugin."""
        if not self.workflow.data.buildargs:
            self.log.info('No buildargs specified, skipping plugin')
            return

        self.workflow.build_dir.for_each_platform(self.add_buildargs)
