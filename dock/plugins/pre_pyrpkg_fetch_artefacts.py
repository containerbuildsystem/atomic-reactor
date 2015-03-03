"""
To have everything for a build in dist-git you need to fetch artefacts using 'fedpkg sources'.

This plugin should do it.
"""
import os
import subprocess

from dock.plugin import PreBuildPlugin


class DistgitFetchArtefactsPlugin(PreBuildPlugin):
    key = "distgit_fetch_artefacts"
    can_fail = False

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
        sources_file_path = os.path.join(self.workflow.builder.git_path, 'sources')
        artefacts = ""
        try:
            with open(sources_file_path, 'r') as f:
                artefacts = f.read()
                self.log.info('Sources file:\n%s', artefacts)
        except IOError as ex:
            if ex.errno == 2:
                self.log.info("no sources file")
            else:
                raise

        subprocess.check_call([self.binary, "--path", self.workflow.builder.git_path, "sources",
                               "--outdir", self.workflow.builder.git_path])
        return artefacts
