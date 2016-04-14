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
    import ConfigParser as configparser
    # We import BytesIO as StringIO as configparser can't properly write
    from io import BytesIO, BytesIO as StringIO
except ImportError:
    # py3
    from urllib.parse import unquote, urlsplit
    import configparser
    from io import BytesIO, StringIO


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

    def is_valid(self):
        # Using BytesIO as configparser in 2.7 can't work with unicode
        # see http://bugs.python.org/issue11597
        with BytesIO(self.content) as buf:
            self.config = configparser.ConfigParser()
            try:
                # Try python3 method
                try:
                    self.config.read_string(self.content.decode('unicode_escape'))
                except AttributeError:
                    # Fallback to py2 method
                    self.config.readfp(buf)
            except configparser.Error:
                self.log.warn("Invalid repo file found: '%s'", self.content)
                return False
            else:
                return True

    def set_proxy_for_all_repos(self, proxy_name):
        for section in self.config.sections():
            self.config.set(section, 'proxy', proxy_name)

        with StringIO() as output:
            self.config.write(output)
            self.content = output.getvalue()


class AddYumRepoByUrlPlugin(PreBuildPlugin):
    key = "add_yum_repo_by_url"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, repourls, inject_proxy=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param repourls: list of str, URLs to the repo files
        :param inject_proxy: set proxy server for this repo
        """
        # call parent constructor
        super(AddYumRepoByUrlPlugin, self).__init__(tasker, workflow)
        self.repourls = repourls
        self.inject_proxy = inject_proxy

    def run(self):
        """
        run the plugin
        """
        if self.repourls:
            for repourl in self.repourls:
                yumrepo = YumRepo(repourl)
                yumrepo.fetch()
                self.log.info("fetched repo from '%s'", yumrepo.repourl)
                if self.inject_proxy:
                    if yumrepo.is_valid():
                        yumrepo.set_proxy_for_all_repos(self.inject_proxy)
                self.workflow.files[yumrepo.dst_filename] = yumrepo.content
                self.log.debug("saving repo '%s', length %d", yumrepo.dst_filename, len(yumrepo.content))
