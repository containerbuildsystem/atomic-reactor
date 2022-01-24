"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import time
from typing import Optional, Tuple

from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugin import PreBuildPlugin
from osbs.utils import Labels
from atomic_reactor.plugins.pre_fetch_sources import PLUGIN_FETCH_SOURCES_KEY
from atomic_reactor.constants import (PLUGIN_BUMP_RELEASE_KEY, PROG, KOJI_RESERVE_MAX_RETRIES,
                                      KOJI_RESERVE_RETRY_DELAY)
from atomic_reactor.config import get_koji_session
from atomic_reactor.util import is_scratch_build
from koji import GenericError
import koji


class BumpReleasePlugin(PreBuildPlugin):
    """
    When there is no release label set, create one by asking Koji what
    the next release should be.
    """

    key = PLUGIN_BUMP_RELEASE_KEY
    is_allowed_to_fail = False  # We really want to stop the process

    @staticmethod
    def args_from_user_params(user_params: dict) -> dict:
        flatpak = user_params.get("flatpak")
        isolated = user_params.get("isolated")
        if flatpak and not isolated:
            return {"append": True}
        return {}

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

    def next_release_general(
        self, component: str, version: str, release: Optional[str] = None
    ) -> str:
        """Get next release for build."""
        if is_scratch_build(self.workflow):
            # no need to append for scratch build
            next_release = self.workflow.user_params.get('pipeline_run_name')
        elif self.append:
            next_release = self.get_next_release_append(component, version, release)
        else:
            next_release = self.get_next_release_standard(component, version)

        return next_release

    def get_next_release_standard(self, component: str, version: str) -> str:
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

    def get_next_release_append(
        self, component: str, version: str, base_release: Optional[str], base_suffix: int = 1
    ) -> str:
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

    def reserve_build_in_koji(
        self,
        component: str,
        version: str,
        release: str,
        source_build: bool = False,
        explicitly_provided_release: bool = False,
    ) -> str:
        """Reserve build in koji, and set reserved build id an token in workflow for koji_import.

        :param component: the component label from the Dockerfile
        :param version: the version label from the Dockerfile
        :param release: the release value (from the Dockerfile / from user params / auto-bumped)
        :param source_build: True if this is a source container build
        :param explicitly_provided_release: True if the release value was explicitly specified,
            either in the Dockerfile or in user_params (i.e. via CLI)
        """
        next_release = release

        for counter in range(KOJI_RESERVE_MAX_RETRIES + 1):
            nvr_data = {
                'name': component,
                'version': version,
                'release': next_release,
            }

            try:
                self.log.info("reserving build in koji: %r", nvr_data)
                reserve = self.xmlrpc.CGInitBuild(PROG, nvr_data)
                break
            except GenericError as exc:
                if explicitly_provided_release:
                    self.log.error(
                        "CGInitBuild failed, not retrying because release was explicitly provided "
                        "by user (in Dockerfile labels or via CLI option)"
                    )
                    raise RuntimeError(exc) from exc

                if counter < KOJI_RESERVE_MAX_RETRIES:
                    self.log.info("retrying CGInitBuild")
                    time.sleep(KOJI_RESERVE_RETRY_DELAY)
                    if not source_build:
                        next_release = self.next_release_general(component, version)
                    else:
                        base_rel, base_suffix = next_release.rsplit('.', 1)
                        next_release = self.get_next_release_append(
                            component, version, base_rel, base_suffix=int(base_suffix) + 1
                        )
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

        return next_release

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
                self.reserve_build_in_koji(
                    source_nvr['name'],
                    source_nvr['version'],
                    source_nvr['release'],
                    source_build=True,
                )

            return

        # any_platform: the N-V-R labels should be equal for all platforms
        dockerfile_labels = self.workflow.build_dir.any_platform.dockerfile_with_parent_env(
            self.workflow.imageutil.base_image_inspect()
        ).labels

        component, version, original_release = self._get_nvr(dockerfile_labels)

        # Reserve build for isolated builds as well (or any build with supplied release)
        user_provided_release = self.workflow.user_params.get('release')
        if user_provided_release:
            if is_scratch_build(self.workflow):
                return

            self.check_build_existence_for_explicit_release(component, version,
                                                            user_provided_release)

            if self.reserve_build:
                self.reserve_build_in_koji(
                    component, version, user_provided_release, explicitly_provided_release=True
                )
            return

        if original_release and not self.append:
            self.log.debug("release set explicitly so not incrementing")
            release = original_release

            if not is_scratch_build(self.workflow):
                self.check_build_existence_for_explicit_release(component, version, release)
        else:
            # release not set or release should be appended
            release = self.next_release_general(component, version, original_release)

        if self.reserve_build and not is_scratch_build(self.workflow):
            release = self.reserve_build_in_koji(
                component, version, release, explicitly_provided_release=bool(original_release)
            )

        # Always set preferred release label - other will be set if old-style label is present
        preferred_release_label = Labels.LABEL_NAMES[Labels.LABEL_TYPE_RELEASE][0]

        def set_release_in_df(build_dir: BuildDir):
            # Update the Dockerfile (dockerfile.labels.__setitem__ writes to the file)
            build_dir.dockerfile.labels[preferred_release_label] = release

        if dockerfile_labels.get(preferred_release_label) != release:
            self.log.info("setting %s=%s", preferred_release_label, release)
            self.workflow.build_dir.for_each_platform(set_release_in_df)

    def _get_nvr(self, dockerfile_labels) -> Tuple[str, str, Optional[str]]:
        """Get the component, version and release labels from the Dockerfile."""
        labels = Labels(dockerfile_labels)

        component_label = labels.get_name(Labels.LABEL_TYPE_COMPONENT)
        component: Optional[str] = dockerfile_labels.get(component_label)

        version_label = labels.get_name(Labels.LABEL_TYPE_VERSION)
        version: Optional[str] = dockerfile_labels.get(version_label)

        release_label = labels.get_name(Labels.LABEL_TYPE_RELEASE)
        release: Optional[str] = dockerfile_labels.get(release_label)

        missing_labels = {}

        # component, version: must be present and not empty
        for label, value in (component_label, component), (version_label, version):
            if value is None:
                self.log.error("missing label: %s", label)
                missing_labels[label] = "missing"
            elif not value:
                self.log.error("empty label: %s", label)
                missing_labels[label] = "empty"

        # release: if present, must not be empty
        if (release is not None) and not release:
            self.log.error("empty label: %s", release_label)
            missing_labels[release_label] = "empty"

        if missing_labels:
            raise RuntimeError(
                "Required labels are missing or empty or using undefined variables: {}"
                .format(missing_labels)
            )

        # For type-checkers: narrow the type of component and version to str
        assert component is not None and version is not None

        return component, version, release
