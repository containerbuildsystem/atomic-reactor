"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pre build plugin for koji build system
"""
import os
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.constants import YUM_REPOS_DIR
from atomic_reactor.utils.yum import YumRepo
from atomic_reactor.util import render_yum_repo
from atomic_reactor.config import get_koji_session


class KojiPlugin(PreBuildPlugin):
    key = "koji"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, target=None):
        """
        constructor

        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param target: string, koji target to use as a source
        """
        # call parent constructor
        super(KojiPlugin, self).__init__(tasker, workflow)
        self.target = target

        self.xmlrpc = get_koji_session(self.workflow.conf)
        self.pathinfo = self.workflow.conf.koji_path_info
        self.proxy = self.workflow.conf.yum_proxy

    def run(self):
        """
        run the plugin
        """
        if (not self.workflow.user_params.get('include_koji_repo') and
                self.workflow.user_params.get('yum_repourls')):
            self.log.info('there is a yum repo user parameter, skipping plugin')
            return

        if not self.target:
            self.log.info('no target provided, skipping plugin')
            return

        if (self.workflow.dockerfile_images.base_from_scratch and
                not self.workflow.dockerfile_images):
            self.log.info("from scratch single stage can't add repos from koji target")
            return

        target_info = self.xmlrpc.getBuildTarget(self.target)
        if target_info is None:
            self.log.error("provided target '%s' doesn't exist", self.target)
            raise RuntimeError("Provided target '%s' doesn't exist!" % self.target)
        tag_info = self.xmlrpc.getTag(target_info['build_tag_name'])

        if not tag_info or 'name' not in tag_info:
            self.log.warning("No tag info was retrieved")
            return

        repo_info = self.xmlrpc.getRepo(tag_info['id'])

        if not repo_info or 'id' not in repo_info:
            self.log.warning("No repo info was retrieved")
            return

        # to use urljoin, we would have to append '/', so let's append everything
        baseurl = self.pathinfo.repo(repo_info['id'], tag_info['name']) + "/$basearch"

        self.log.info("baseurl = '%s'", baseurl)

        repo = {
            'name': 'atomic-reactor-koji-plugin-%s' % self.target,
            'baseurl': baseurl,
            'enabled': 1,
            'gpgcheck': 0,
        }

        # yum doesn't accept a certificate path in sslcacert - it requires a db with added cert
        # dnf ignores that option completely
        # we have to fall back to sslverify=0 everytime we get https repo from brew so we'll surely
        # be able to pull from it

        if baseurl.startswith("https://"):
            self.log.info("Ignoring certificates in the repo")
            repo['sslverify'] = 0

        if self.proxy:
            self.log.info("Setting yum proxy to %s", self.proxy)
            repo['proxy'] = self.proxy

        path = YumRepo(os.path.join(YUM_REPOS_DIR, self.target)).dst_filename
        self.log.info("yum repo of koji target: '%s'", path)
        self.workflow.files[path] = render_yum_repo(repo, escape_dollars=False)
