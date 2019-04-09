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
from __future__ import absolute_import

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.yum_util import YumRepo


class AddYumRepoByUrlPlugin(PreBuildPlugin):
    key = "add_yum_repo_by_url"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, repourls=None, inject_proxy=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param repourls: list of str, URLs to the repo files
        :param inject_proxy: set proxy server for this repo
        """
        # call parent constructor
        super(AddYumRepoByUrlPlugin, self).__init__(tasker, workflow)
        self.repourls = repourls or []
        self.inject_proxy = inject_proxy

    def run(self):
        """
        run the plugin
        """
        if self.workflow.builder.base_from_scratch and not self.workflow.builder.parent_images:
            self.log.info("Skipping add yum repo by url: unsupported for FROM-scratch images")
            return

        if self.repourls:
            for repourl in self.repourls:
                yumrepo = YumRepo(repourl)
                self.log.info("fetching yum repo from '%s'", yumrepo.repourl)
                try:
                    yumrepo.fetch()
                except Exception as e:
                    msg = "Failed to fetch yum repo {repo}: {exc}".format(
                        repo=yumrepo.repourl, exc=e)
                    raise RuntimeError(msg)
                else:
                    self.log.info("fetched yum repo from '%s'", yumrepo.repourl)

                if self.inject_proxy:
                    if yumrepo.is_valid():
                        yumrepo.set_proxy_for_all_repos(self.inject_proxy)
                self.workflow.files[yumrepo.dst_filename] = yumrepo.content
                self.log.debug("saving yum repo '%s', length %d", yumrepo.dst_filename,
                               len(yumrepo.content))
