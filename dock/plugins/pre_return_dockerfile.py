"""
It returns the dockerfile itself and therefore displays it in results.
"""
from dock.plugin import PreBuildPlugin


class CpDockerfilePlugin(PreBuildPlugin):
    key = "dockerfile_content"

    def __init__(self, tasker, workflow, path):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :return:
        """
        # call parent constructor
        super(CpDockerfilePlugin, self).__init__(tasker, workflow)

    def run(self):
        """
        try open dockerfile, output an error if there is one
        """
        try:
            with open(self.workflow.builder.df_path, 'r') as fd:
                return fd.read()
        except (IOError, OSError) as ex:
            msg = "Couldn't copy dockerfile: %s" % repr(ex)
            self.log.error(msg)
            return msg
