"""
Add arbitrary yum repo, specified by URL of repo file, to a list of
repos which should be injected into built image.

This plugin has to run _BEFORE_ yum inject plugin.

Example configuration to add content of repo file at URL:

{
    "name": "add_yum_repo_by_url",
    "args": {
        "repourls": ["http://example.com/myrepo/myrepo.repo"]
    }
}

"""
from dock.plugin import PreBuildPlugin
import requests
try:
    # py2
    from StringIO import StringIO
    # use StringIO, not cStringIO
    # cStringIO's readlines() gives bad results for unicode objects
    from ConfigParser import SafeConfigParser
except ImportError:
    # py3
    from io import StringIO
    from configparser import SafeConfigParser


class AddYumRepoByUrlPlugin(PreBuildPlugin):
    key = "add_yum_repo_by_url"
    can_fail = False

    def __init__(self, tasker, workflow, repourls):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param repourls: list of str, URLs to the repo files
        """
        # call parent constructor
        super(AddYumRepoByUrlPlugin, self).__init__(tasker, workflow)
        self.repourls = repourls

    @staticmethod
    def _get(url):
        return requests.get(url)

    def run(self):
        """
        run the plugin
        """
        self.workflow.repos.setdefault("yum", [])
        for repourl in self.repourls:
            repoconfig = SafeConfigParser()
            response = self._get(repourl)
            response.raise_for_status()
            repoconfig.readfp(StringIO(response.text))
            for name in repoconfig.sections():
                repo = dict(repoconfig.items(name))
                repo['name'] = name
                self.workflow.repos['yum'].append(repo)
