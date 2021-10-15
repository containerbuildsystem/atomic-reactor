"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import time
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import df_parser
from osbs.utils import Labels
from atomic_reactor.plugins.pre_fetch_sources import PLUGIN_FETCH_SOURCES_KEY
from atomic_reactor.constants import (PLUGIN_BUMP_RELEASE_KEY, PROG, KOJI_RESERVE_MAX_RETRIES,
                                      KOJI_RESERVE_RETRY_DELAY)
from atomic_reactor.config import get_koji_session
from atomic_reactor.util import is_scratch_build, map_to_user_params
from koji import GenericError
import koji


class BumpReleasePlugin(PreBuildPlugin):
    """
    When there is no release label set, create one by asking Koji what
    the next release should be.
    """

    key = PLUGIN_BUMP_RELEASE_KEY
    is_allowed_to_fail = False  # We really want to stop the process

    args_from_user_params = map_to_user_params("append:flatpak")

    # The target parameter is no longer used by this plugin. It's
    # left as an optional parameter to allow a graceful transition
    # in osbs-client.
    def __init__(self, workflow, append=False):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :param append: if True, the release will be obtained by appending a
            '.' and a unique integer to the release label in the dockerfile.
        """
        # call parent constructor
        super(BumpReleasePlugin, self).__init__(workflow)

        self.append = append
        self.xmlrpc = get_koji_session(self.workflow.conf)
        koji_setting = self.workflow.conf.koji
        self.reserve_build = koji_setting.get('reserve_build', False)

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

    def next_release_general(self, component, version, release, release_label,
                             dockerfile_labels):
        """
        get next release for build and set it in dockerfile
        """
        if is_scratch_build(self.workflow):
            # no need to append for scratch build
            next_release = self.workflow.user_params.get('pipeline_run_name')
        elif self.append:
            next_release = self.get_next_release_append(component, version, release)
        else:
            next_release = self.get_next_release_standard(component, version)

        # No release labels are set so set them
        self.log.info("setting %s=%s", release_label, next_release)
        # Write the label back to the file (this is a property setter)
        dockerfile_labels[release_label] = next_release

    def get_next_release_standard(self, component, version):
        build_info = {'name': component, 'version': version}
        self.log.debug('getting next release from build info: %s', build_info)
        try:
            next_release = self.get_patched_release(self.xmlrpc.getNextRelease(build_info))
        # when release can't be bumped via koji's getNextRelease (unsupported format,
        # eg. multiple dots, strings etc) it will raise exception
        except koji.BuildError:
            next_release = self.get_next_release(build_info)

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
            elif self.reserve_build:
                if build['state'] in (koji.BUILD_STATES['FAILED'], koji.BUILD_STATES['CANCELED']):
                    return next_release

            next_release = self.get_patched_release(next_release, increment=True)

    def get_next_release_append(self, component, version, base_release, base_suffix=1):
        # This is brute force, but trying to use getNextRelease() would be fragile
        # magic depending on the exact details of how koji increments the release,
        # and we expect that the number of builds for any one base_release will be small.
        release = base_release or '1'
        suffix = base_suffix
        while True:
            next_release = '%s.%s' % (release, suffix)
            build_info = {'name': component, 'version': version, 'release': next_release}
            self.log.debug('checking that the build does not exist: %s', build_info)
            build = self.xmlrpc.getBuild(build_info)
            if not build:
                return next_release
            elif self.reserve_build:
                if build['state'] in (koji.BUILD_STATES['FAILED'], koji.BUILD_STATES['CANCELED']):
                    return next_release
            suffix += 1

    def get_next_release(self, build_info):
        queryopts = {'order': '-build.id', 'limit': 1}
        # release is either single number or with decimal point, so we can easily bump next release
        search_str = r'^{}-{}-\d+(\.\d+)?$'.format(build_info['name'], build_info['version'])

        query_builds = self.xmlrpc.search(search_str, 'build', 'regexp', queryOpts=queryopts)
        # if query did not find any build, we will use release 1
        next_release = '1'
        if query_builds:
            build = self.xmlrpc.getBuild(query_builds[0]['id'])
            next_release = self.get_patched_release(build['release'], increment=True)

        return next_release

    def reserve_build_in_koji(self, component, version, release, release_label,
                              dockerfile_labels, source_build=False, user_provided_release=False):
        """
        reserve build in koji, and set reserved build id an token in workflow
        for koji_import
        """
        for counter in range(KOJI_RESERVE_MAX_RETRIES + 1):
            nvr_data = {
                'name': component,
                'version': version,
            }
            if source_build or user_provided_release:
                nvr_data['release'] = release
            else:
                nvr_data['release'] = dockerfile_labels[release_label]

            try:
                self.log.info("reserving build in koji: %r", nvr_data)
                reserve = self.xmlrpc.CGInitBuild(PROG, nvr_data)
                break
            except GenericError as exc:
                if release and user_provided_release:
                    self.log.error("CGInitBuild failed, not retrying because"
                                   " release was explicitly provided by user")
                    raise RuntimeError(exc) from exc

                if release and not source_build:
                    self.log.error("CGInitBuild failed, not retrying because"
                                   " release was explicitly specified in Dockerfile")
                    raise RuntimeError(exc) from exc

                if counter < KOJI_RESERVE_MAX_RETRIES:
                    self.log.info("retrying CGInitBuild")
                    time.sleep(KOJI_RESERVE_RETRY_DELAY)
                    if not source_build:
                        self.next_release_general(component, version, release,
                                                  release_label, dockerfile_labels)
                    else:
                        base_rel, base_suffix = release.rsplit('.', 1)
                        release = self.get_next_release_append(component, version, base_rel,
                                                               base_suffix=int(base_suffix) + 1)
                else:
                    self.log.error("CGInitBuild failed, reached maximum number of retries %s",
                                   KOJI_RESERVE_MAX_RETRIES)
                    raise RuntimeError(exc) from exc
            except Exception:
                self.log.error("CGInitBuild failed")
                raise

        self.workflow.reserved_build_id = reserve['build_id']
        self.workflow.reserved_token = reserve['token']
        if source_build:
            self.workflow.koji_source_nvr = nvr_data

    def check_build_existence_for_explicit_release(self, component, version, release):
        build_info = {'name': component, 'version': version, 'release': release}
        self.log.debug('checking that the build does not exist: %s', build_info)
        build = self.xmlrpc.getBuild(build_info)
        if build:
            if self.reserve_build:
                if build['state'] in (koji.BUILD_STATES['FAILED'],
                                      koji.BUILD_STATES['CANCELED']):
                    return

            raise RuntimeError('build already exists in Koji: {}-{}-{} ({})'
                               .format(component, version, release, build.get('id')))

    def get_source_build_nvr(self, scratch=False):
        source_result = self.workflow.prebuild_results[PLUGIN_FETCH_SOURCES_KEY]
        koji_build_nvr = source_result['sources_for_nvr']

        koji_build = self.xmlrpc.getBuild(koji_build_nvr)
        build_component = "%s-source" % koji_build['name']
        build_version = koji_build['version']
        build_release = koji_build['release']
        self.workflow.koji_source_source_url = koji_build['source']

        if not scratch:
            next_release = self.get_next_release_append(build_component, build_version,
                                                        build_release)
        else:
            # for scratch source release we will just use original release with scratch string
            next_release = "%s.scratch" % koji_build['release']
        return {'name': build_component, 'version': build_version, 'release': next_release}

    def run(self):
        """
        run the plugin
        """
        # source container build
        if PLUGIN_FETCH_SOURCES_KEY in self.workflow.prebuild_results:
            source_nvr = self.get_source_build_nvr(scratch=is_scratch_build(self.workflow))
            self.log.info("Setting source_build_nvr: %s", source_nvr)
            self.workflow.koji_source_nvr = source_nvr

            if self.reserve_build and not is_scratch_build(self.workflow):
                self.reserve_build_in_koji(source_nvr['name'], source_nvr['version'],
                                           source_nvr['release'], None, None, source_build=True)

            return

        parser = df_parser(self.workflow.df_path, workflow=self.workflow)
        dockerfile_labels = parser.labels
        labels = Labels(dockerfile_labels)
        missing_labels = {}
        missing_value = 'missing'
        empty_value = 'empty'

        component_label = labels.get_name(Labels.LABEL_TYPE_COMPONENT)
        try:
            component = dockerfile_labels[component_label]
        except KeyError:
            self.log.error("%s label: %s", missing_value, component_label)
            missing_labels[component_label] = missing_value

        version_label = labels.get_name(Labels.LABEL_TYPE_VERSION)
        try:
            version = dockerfile_labels[version_label]
            if not version:
                self.log.error('%s label: %s', empty_value, version_label)
                missing_labels[version_label] = empty_value
        except KeyError:
            self.log.error('%s label: %s', missing_value, version_label)
            missing_labels[version_label] = missing_value

        try:
            release_label, release = labels.get_name_and_value(Labels.LABEL_TYPE_RELEASE)
        except KeyError:
            release = None
        else:
            if not release:
                self.log.error('%s label: %s', empty_value, release_label)
                missing_labels[release_label] = empty_value

        if missing_labels:
            raise RuntimeError('Required labels are missing or empty or using'
                               ' undefined variables: {}'.format(missing_labels))

        # Always set preferred release label - other will be set if old-style
        # label is present
        release_label = labels.LABEL_NAMES[Labels.LABEL_TYPE_RELEASE][0]

        # Reserve build for isolated builds as well (or any build with supplied release)
        user_provided_release = self.workflow.user_params.get('release')
        if user_provided_release:
            if is_scratch_build(self.workflow):
                return

            self.check_build_existence_for_explicit_release(component, version,
                                                            user_provided_release)

            if self.reserve_build:
                self.reserve_build_in_koji(component, version, user_provided_release,
                                           release_label, dockerfile_labels,
                                           user_provided_release=True)
            return

        if release:
            if not self.append:
                self.log.debug("release set explicitly so not incrementing")

                if not is_scratch_build(self.workflow):
                    self.check_build_existence_for_explicit_release(component, version, release)
                    dockerfile_labels[release_label] = release
                else:
                    return

        if not release or self.append:
            self.next_release_general(component, version, release, release_label,
                                      dockerfile_labels)

        if self.reserve_build and not is_scratch_build(self.workflow):
            self.reserve_build_in_koji(component, version, release, release_label,
                                       dockerfile_labels)
