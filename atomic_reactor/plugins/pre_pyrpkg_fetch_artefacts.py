"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


To have everything for a build in dist-git you need to fetch artefacts using 'fedpkg sources'.

This plugin should do it.
"""
import os
import subprocess

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.constants import PLUGIN_DISTGIT_FETCH_KEY


class DistgitFetchArtefactsPlugin(PreBuildPlugin):
    key = PLUGIN_DISTGIT_FETCH_KEY
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow):
        """
        constructor

        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param command: str, command to use to get artefacts (e.g. 'make sources')
                             it is executed in cloned git repo
        """
        # call parent constructor
        super(DistgitFetchArtefactsPlugin, self).__init__(tasker, workflow)
        self.command = self.workflow.conf.sources_command

    def run(self):
        """
        fetch artefacts
        """
        if not self.command:
            self.log.info('no sources command configuration, skipping plugin')
            return

        source_path = self.workflow.source.path
        cur_dir = os.getcwd()
        os.chdir(source_path)
        try:
            subprocess.check_call(self.command.split())
        finally:
            os.chdir(cur_dir)
