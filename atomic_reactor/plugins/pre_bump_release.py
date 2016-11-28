"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import get_all_label_keys, get_preferred_label_key, df_parser
from atomic_reactor.koji_util import create_koji_session


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

        parser = df_parser(self.workflow.builder.df_path, workflow=self.workflow)
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

        # getNextRelease will return the release of the last successful build
        # but next_release might be a failed build. Koji's CGImport doesn't
        # allow reuploading builds, so instead we should increment next_release
        # and make sure the build doesn't exist
        while True:
            build_info = {'name': component, 'version': version, 'release': next_release}
            self.log.debug('checking that the build does not exist: %s', build_info)
            build = self.xmlrpc.getBuild(build_info)
            if not build:
                break

            next_release = str(int(next_release) + 1)

        # Always set preferred release label - other will be set if old-style
        # label is present
        preferred_release_label = get_preferred_label_key(dockerfile_labels,
                                                         'release')
        old_style_label = get_all_label_keys('com.redhat.component')[1]
        release_labels_to_be_set = [preferred_release_label]
        if old_style_label in dockerfile_labels.keys():
            release_labels_to_be_set = release_labels

        # No release labels are set so set them
        for release_label in release_labels_to_be_set:
            self.log.info("setting %s=%s", release_label, next_release)

            # Write the label back to the file (this is a property setter)
            dockerfile_labels[release_label] = next_release
