"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Add arbitrary yum repo, specified by URL of repo file, to a list of
repos which should be injected into built image by the inject_yum_repo
plugin.

This plugin has to run _BEFORE_ the inject_yum_repo plugin, which
actually places the repo file in the build environment.

Example configuration to add content of repo file at URL:

{
    "name": "add_yum_repo_by_url",
    "args": {
        "repourls": ["http://example.com/myrepo/myrepo.repo"]
    }
}

"""
from atomic_reactor.constants import YUM_REPOS_DIR
from atomic_reactor.plugin import PreBuildPlugin
import os
import os.path
import requests

try:
    # py2
    from urlparse import unquote, urlsplit
except ImportError:
    # py3
    from urllib.parse import unquote, urlsplit


class YumRepo(object):
    def __init__(self, repourl, dst_repos_dir=YUM_REPOS_DIR):
        self.repourl = repourl
        self.dst_repos_dir = dst_repos_dir
        self.content = None

    @property
    def filename(self):
        urlpath = unquote(urlsplit(self.repourl, allow_fragments=False).path)
        return os.path.basename(urlpath)

    @property
    def dst_filename(self):
        return os.path.join(self.dst_repos_dir, self.filename)

    def fetch(self):
        response = requests.get(self.repourl)
        response.raise_for_status()
        self.content = response.content


class AddYumRepoByUrlPlugin(PreBuildPlugin):
    key = "add_yum_repo_by_url"
    is_allowed_to_fail = False

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

    def run(self):
        """
        run the plugin
        """
        if self.repourls:
            for repourl in self.repourls:
                yumrepo = YumRepo(repourl)
                yumrepo.fetch()
                self.log.info("fetched repo from '%s'", yumrepo.repourl)
                self.workflow.files[yumrepo.dst_filename] = yumrepo.content
                self.log.debug("saving repo '%s', length %d", yumrepo.dst_filename, len(yumrepo.content))
