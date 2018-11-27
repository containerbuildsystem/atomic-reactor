"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import df_parser
from osbs.utils import Labels
from atomic_reactor.plugins.pre_reactor_config import get_koji_session
from atomic_reactor.constants import PLUGIN_BUMP_RELEASE_KEY
from atomic_reactor.util import get_build_json, is_scratch_build


class BumpReleasePlugin(PreBuildPlugin):
    """
    When there is no release label set, create one by asking Koji what
    the next release should be.
    """

    key = PLUGIN_BUMP_RELEASE_KEY
    is_allowed_to_fail = False  # We really want to stop the process

    # The target parameter is no longer used by this plugin. It's
    # left as an optional parameter to allow a graceful transition
    # in osbs-client.
    def __init__(self, tasker, workflow, hub=None, target=None, koji_ssl_certs_dir=None,
                 append=False):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param hub: string, koji hub (xmlrpc)
        :param target: unused - backwards compatibility
        :param koji_ssl_certs_dir: str, path to "cert", "ca", and "serverca"
            Note that this plugin requires koji_ssl_certs_dir set if Koji
            certificate is not trusted by CA bundle.
        :param append: if True, the release will be obtained by appending a
            '.' and a unique integer to the release label in the dockerfile.
        """
        # call parent constructor
        super(BumpReleasePlugin, self).__init__(tasker, workflow)

        self.koji_fallback = {
            'hub_url': hub,
            'auth': {
                'ssl_certs_dir': koji_ssl_certs_dir,
            }
        }
        self.append = append
        self.xmlrpc = get_koji_session(self.workflow, self.koji_fallback)

    def get_patched_release(self, original_release, increment=False):
        # Split the original release by dots, make sure there at least 3 items in parts list
        parts = original_release.split('.', 2) + [None, None]
        release, suffix, rest = parts[:3]

        if increment:
            # Increment first part as a number
            release = str(int(release) + 1)

        # Remove second part if it's a number
        if suffix is not None and suffix.isdigit():
            suffix = None

        # Recombine the parts
        return '.'.join([part for part in [release, suffix, rest]
                         if part is not None])

    def get_next_release_standard(self, component, version):
        build_info = {'name': component, 'version': version}
        self.log.debug('getting next release from build info: %s', build_info)
        next_release = self.get_patched_release(self.xmlrpc.getNextRelease(build_info))

        # getNextRelease will return the release of the last successful build
        # but next_release might be a failed build. Koji's CGImport doesn't
        # allow reuploading builds, so instead we should increment next_release
        # and make sure the build doesn't exist
        while True:
            build_info = {'name': component, 'version': version, 'release': next_release}
            self.log.debug('checking that the build does not exist: %s', build_info)
            build = self.xmlrpc.getBuild(build_info)
            if not build:
                return next_release

            next_release = self.get_patched_release(next_release, increment=True)

    def get_next_release_append(self, component, version, base_release):
        # This is brute force, but trying to use getNextRelease() would be fragile
        # magic depending on the exact details of how koji increments the release,
        # and we expect that the number of builds for any one base_release will be small.
        release = base_release or '1'
        suffix = 1
        while True:
            next_release = '%s.%s' % (release, suffix)
            build_info = {'name': component, 'version': version, 'release': next_release}
            self.log.debug('checking that the build does not exist: %s', build_info)
            build = self.xmlrpc.getBuild(build_info)
            if not build:
                return next_release

            suffix += 1

    def run(self):
        """
        run the plugin
        """

        parser = df_parser(self.workflow.builder.df_path, workflow=self.workflow)
        dockerfile_labels = parser.labels
        labels = Labels(dockerfile_labels)

        try:
            _, release = labels.get_name_and_value(Labels.LABEL_TYPE_RELEASE)
            if not self.append:
                self.log.debug("release set explicitly so not incrementing")
                return
        except KeyError:
            release = None

        component_label = labels.get_name(Labels.LABEL_TYPE_COMPONENT)

        try:
            component = dockerfile_labels[component_label]
        except KeyError:
            raise RuntimeError("missing label: {}".format(component_label))

        version_label = labels.get_name(Labels.LABEL_TYPE_VERSION)
        try:
            version = dockerfile_labels[version_label]
        except KeyError:
            raise RuntimeError('missing label: {}'.format(version_label))

        if self.append:
            next_release = self.get_next_release_append(component, version, release)
        elif is_scratch_build():
            metadata = get_build_json().get("metadata", {})
            next_release = metadata.get("name", "1")
        else:
            next_release = self.get_next_release_standard(component, version)

        # Always set preferred release label - other will be set if old-style
        # label is present
        release_label = labels.LABEL_NAMES[Labels.LABEL_TYPE_RELEASE][0]

        # No release labels are set so set them
        self.log.info("setting %s=%s", release_label, next_release)
        # Write the label back to the file (this is a property setter)
        dockerfile_labels[release_label] = next_release
