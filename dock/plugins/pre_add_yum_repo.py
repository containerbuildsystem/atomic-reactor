"""
Add arbitrary yum repo to a list of repos which should be injected into built image.

This plugin has to run _BEFORE_ yum inject plugin.
"""
from dock.plugin import PreBuildPlugin


class AddYumRepoPlugin(PreBuildPlugin):
    key = "add_yum_repo"

    def __init__(self, tasker, workflow, repo_name, baseurl):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param repo_name: str, name of yum repo
        :param baseurl: str, URL to the repo
        """
        # call parent constructor
        super(AddYumRepoPlugin, self).__init__(tasker, workflow)
        self.baseurl = baseurl
        self.repo_name = repo_name

    def run(self):
        """
        run the plugin
        """
        self.workflow.repos.setdefault("yum", [])
        repo = {
            'name': self.repo_name,
            'baseurl': self.baseurl,
            'enabled': 1,
            'gpgcheck': 0,
        }
        self.workflow.repos['yum'].append(repo)
