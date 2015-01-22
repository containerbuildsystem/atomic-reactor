"""
To have everything for a build in dist-git you need to fetch artefacts using 'fedpkg sources'.

This plugin should do it.
"""
import subprocess

from dock.plugin import PreBuildPlugin


class DistgitFetchArtefactsPlugin(PreBuildPlugin):
    key = "distgit_fetch_artefacts"

    def __init__(self, tasker, workflow, binary):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param binary: str, name of binary to use (e.g. fedpkg)
        """
        # call parent constructor
        super(DistgitFetchArtefactsPlugin, self).__init__(tasker, workflow)
        self.binary = binary

    def run(self):
        """
        fetch artefacts
        """
        subprocess.check_call([self.binary, "--path", self.workflow.builder.git_path, "sources"])
