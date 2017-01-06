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


class DistgitFetchArtefactsPlugin(PreBuildPlugin):
    key = "distgit_fetch_artefacts"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, command):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param command: str, command to use to get artefacts (e.g. 'make sources')
                             it is executed in cloned git repo
        """
        # call parent constructor
        super(DistgitFetchArtefactsPlugin, self).__init__(tasker, workflow)
        self.command = command

    def run(self):
        """
        fetch artefacts
        """
        source_path = self.workflow.source.path
        cur_dir = os.getcwd()
        os.chdir(source_path)
        try:
            subprocess.check_call(self.command.split())
        finally:
            os.chdir(cur_dir)
