"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import get_all_label_keys, get_preferred_label_key
from atomic_reactor.koji_util import create_koji_session
from dockerfile_parse import DockerfileParser


class BumpReleasePlugin(PreBuildPlugin):
    """
    When there is no release label set, create one by asking Koji what
    the next release should be.
    """

    key = "bump_release"
    is_allowed_to_fail = False  # We really want to stop the process

    def __init__(self, tasker, workflow, target, hub):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param target: string, koji target to use as a source
        :param hub: string, koji hub (xmlrpc)
        """
        # call parent constructor
        super(BumpReleasePlugin, self).__init__(tasker, workflow)
        self.target = target
        self.xmlrpc = create_koji_session(hub)

    def run(self):
        """
        run the plugin
        """

        parser = DockerfileParser(self.workflow.builder.df_path)
        release_labels = get_all_label_keys('release')
        dockerfile_labels = parser.labels
        if any(release_label in dockerfile_labels
               for release_label in release_labels):
            self.log.debug("release set explicitly so not incrementing")
            return

        # No release labels are set so set them.
        component_label = get_preferred_label_key(dockerfile_labels,
                                                  'com.redhat.component')
        try:
            component = dockerfile_labels[component_label]
        except KeyError:
            raise RuntimeError("missing label: {}".format(component_label))

        target_info = self.xmlrpc.getBuildTarget(self.target)
        self.log.debug('target info: %s', target_info)
        if not target_info:
            raise RuntimeError("no such target: %s" % self.target)

        tag_id = target_info['dest_tag']
        if not self.xmlrpc.checkTagPackage(tag_id, component):
            raise RuntimeError(
                "package %s is not in the list for target %s" % (component, self.target))

        latest = self.xmlrpc.getLatestBuilds(tag_id, package=component)
        self.log.debug('latest builds: %s', latest)
        try:
            next_release = self.xmlrpc.getNextRelease(latest[0])
        except IndexError:
            next_release = 1

        next_release = str(next_release)
        for release_label in release_labels:
            self.log.info("setting %s=%s", release_label, next_release)

            # Write the label back to the file (this is a property setter).
            dockerfile_labels[release_label] = next_release
