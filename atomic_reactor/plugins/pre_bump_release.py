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

    # The target parameter is no longer used by this plugin. It's
    # left as an optional parameter to allow a graceful transition
    # in osbs-client.
    def __init__(self, tasker, workflow, hub, target=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param hub: string, koji hub (xmlrpc)
        :param target: unused - backwards compatibility
        """
        # call parent constructor
        super(BumpReleasePlugin, self).__init__(tasker, workflow)
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

        component_label = get_preferred_label_key(dockerfile_labels,
                                                  'com.redhat.component')
        try:
            component = dockerfile_labels[component_label]
        except KeyError:
            raise RuntimeError("missing label: {}".format(component_label))

        version_label = get_preferred_label_key(dockerfile_labels, 'version')
        try:
            version = dockerfile_labels[version_label]
        except KeyError:
            raise RuntimeError('missing label: {}'.format(version_label))

        build_info = {'name': component, 'version': version}
        self.log.debug('getting next release from build info: %s', build_info)
        next_release = self.xmlrpc.getNextRelease(build_info)

        # No release labels are set so set them
        for release_label in release_labels:
            self.log.info("setting %s=%s", release_label, next_release)

            # Write the label back to the file (this is a property setter)
            dockerfile_labels[release_label] = next_release
