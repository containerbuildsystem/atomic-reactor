"""Copyright (c) 2018, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

After squashing our image, verify that it has the media types that
the registry expects
"""

from __future__ import unicode_literals, absolute_import

from atomic_reactor.constants import (PLUGIN_GROUP_MANIFESTS_KEY, PLUGIN_VERIFY_MEDIA_KEY,
                                      PLUGIN_FETCH_SOURCES_KEY,
                                      MEDIA_TYPE_DOCKER_V2_SCHEMA1,
                                      MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                                      MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST,
                                      MEDIA_TYPE_OCI_V1,
                                      MEDIA_TYPE_OCI_V1_INDEX)

from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.util import (get_manifest_digests, get_platforms,
                                 is_manifest_list, ManifestDigest)
from atomic_reactor.plugins.pre_reactor_config import (get_registries,
                                                       get_platform_to_goarch_mapping,
                                                       get_source_container)
from copy import deepcopy


class VerifyMediaTypesPlugin(ExitPlugin):
    key = PLUGIN_VERIFY_MEDIA_KEY
    is_allowed_to_fail = False

    def run(self):
        # Only run if the build was successful
        if self.workflow.build_process_failed:
            self.log.info("Not running for failed build")
            return []

        # Work out the name of the image to pull
        if not self.workflow.tag_conf.unique_images:
            raise ValueError("no unique image set, impossible to verify media types")
        image = self.workflow.tag_conf.unique_images[0]

        registries = deepcopy(get_registries(self.workflow, {}))
        media_in_registry = {}
        expect_list_only = self.get_manifest_list_only_expectation()

        for registry_name, registry in registries.items():
            expected_media_types = set(registry.get('expected_media_types', []))
            media_types = set()

            if expect_list_only:
                expected_media_types = {MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST}

            media_in_registry[registry_name] = {'expected': expected_media_types}

            pullspec = image.copy()
            pullspec.registry = registry_name
            insecure = registry.get('insecure', False)
            secret = registry.get('secret', None)

            kwargs = {}
            if PLUGIN_FETCH_SOURCES_KEY in self.workflow.prebuild_results:
                # For source containers, limit the versions we ask
                # about (and, if necessary, the expected media types).
                # This can help to avoid issues with tooling that is
                # unable to deal with the number of layers in these
                # images.
                src_config = get_source_container(self.workflow, fallback={})
                limit_media_types = src_config.get('limit_media_types')
                if limit_media_types is not None:
                    short_name = {v: k for k, v in ManifestDigest.content_type.items()}
                    versions = tuple(short_name[mt] for mt in limit_media_types)
                    kwargs['versions'] = versions

                    if expected_media_types:
                        expected_media_types.intersection_update(set(limit_media_types))

            digests = get_manifest_digests(pullspec, registry_name, insecure,
                                           secret, require_digest=False, **kwargs)
            if digests:
                if digests.v2_list:
                    media_types.add(MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST)
                if digests.v2:
                    media_types.add(MEDIA_TYPE_DOCKER_V2_SCHEMA2)
                if digests.v1:
                    media_types.add(MEDIA_TYPE_DOCKER_V2_SCHEMA1)
                if digests.oci:
                    media_types.add(MEDIA_TYPE_OCI_V1)
                if digests.oci_index:
                    media_types.add(MEDIA_TYPE_OCI_V1_INDEX)

            media_in_registry[registry_name]['found'] = media_types

        should_raise = False
        all_found = set()
        for registry_name, manifests in media_in_registry.items():
            all_found.update(manifests['found'])
            if manifests['expected'] - manifests['found']:
                should_raise = True
                self.log.error("expected media types %s not in available media types %s,"
                               " for registry %s",
                               sorted(manifests['expected'] - manifests['found']),
                               sorted(manifests['found']),
                               registry_name)

        if should_raise:
            raise KeyError("expected media types were not found")

        if expect_list_only:
            return [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]
        return sorted(all_found)

    def get_manifest_list_only_expectation(self):
        """
        Get expectation for manifest list only

        :return: bool, expect manifest list only?
        """
        manifest_results = self.workflow.postbuild_results.get(PLUGIN_GROUP_MANIFESTS_KEY)
        if not manifest_results or not is_manifest_list(manifest_results.get("media_type")):
            self.log.debug('Cannot check if only manifest list digest should be returned '
                           'because group manifests plugin did not run')
            return False

        platforms = get_platforms(self.workflow)
        if not platforms:
            self.log.debug('Cannot check if only manifest list digest should be returned '
                           'because we have no platforms list')
            return False

        try:
            platform_to_goarch = get_platform_to_goarch_mapping(self.workflow)
        except KeyError:
            self.log.debug('Cannot check if only manifest list digest should be returned '
                           'because there are no platform descriptors')
            return False

        for plat in platforms:
            if platform_to_goarch[plat] == 'amd64':
                self.log.debug('amd64 was built, all media types available')
                return False

        self.log.debug('amd64 was not built, only manifest list digest is available')
        return True
