"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Add arbitrary yum repo to a list of repos which should be injected into built image.

This plugin has to run _BEFORE_ the inject_yum_repo plugin, which
actually places the repo file in the build environment.
"""
import os
from atomic_reactor.constants import YUM_REPOS_DIR
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import render_yum_repo


class AddYumRepoPlugin(PreBuildPlugin):
    key = "add_yum_repo"
    is_allowed_to_fail = False

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
        path = os.path.join(YUM_REPOS_DIR, self.repo_name + ".repo")
        self.log.info("yum repo of koji target: '%s'", path)
        self.workflow.files[path] = render_yum_repo(repo)
