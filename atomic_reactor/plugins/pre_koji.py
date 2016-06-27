"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pre build plugin for koji build system
"""
import os
import koji
from atomic_reactor.constants import YUM_REPOS_DIR
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import render_yum_repo, get_preferred_label_key

from dockerfile_parse import DockerfileParser


class KojiPlugin(PreBuildPlugin):
    key = "koji"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, target, hub, root, proxy=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param target: string, koji target to use as a source
        :param hub: string, koji hub (xmlrpc)
        :param root: string, koji root (storage)
        """
        # call parent constructor
        super(KojiPlugin, self).__init__(tasker, workflow)
        self.target = target
        self.xmlrpc = koji.ClientSession(hub)
        self.pathinfo = koji.PathInfo(topdir=root)
        self.proxy = proxy

    def _check_target_and_tag(self):
        self.target_info = self.xmlrpc.getBuildTarget(self.target)
        self.log.debug('target info: %s', self.target_info)
        if not self.target_info:
            self.log.error("provided target '%s' doesn't exist", self.target)
            raise RuntimeError("Provided target '%s' doesn't exist!" % self.target)

        dest_tag_id = self.target_info['dest_tag']
        parser = DockerfileParser(self.workflow.builder.df_path)
        dockerfile_labels = parser.labels
        component_label = get_preferred_label_key(dockerfile_labels,
                                                  'com.redhat.component')
        try:
            component = dockerfile_labels[component_label]
        except KeyError:
            raise RuntimeError("missing label: {}".format(component_label))
        if not self.xmlrpc.checkTagPackage(dest_tag_id, component):
            raise RuntimeError(
                "package %s is not in the list for target %s" % (component, self.target))

    def run(self):
        """
        run the plugin
        """
        self._check_target_and_tag()

        tag_info = self.xmlrpc.getTag(self.target_info['build_tag_name'])
        repo_info = self.xmlrpc.getRepo(tag_info['id'])
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

        path = os.path.join(YUM_REPOS_DIR, self.target + ".repo")
        self.log.info("yum repo of koji target: '%s'", path)
        self.workflow.files[path] = render_yum_repo(repo, escape_dollars=False)
