"""Copyright (c) 2018 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

After squashing our image, verify that it has the media types that
the registry expects
"""

from __future__ import unicode_literals

from atomic_reactor.constants import (PLUGIN_GROUP_MANIFESTS_KEY, PLUGIN_VERIFY_MEDIA_KEY,
                                      MEDIA_TYPE_DOCKER_V1, MEDIA_TYPE_DOCKER_V2_SCHEMA1,
                                      MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                                      MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST)

from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.util import get_manifest_digests, get_platforms, RegistrySession
from atomic_reactor.plugins.pre_reactor_config import (get_registries,
                                                       get_platform_to_goarch_mapping)
from copy import deepcopy
from itertools import chain


def verify_v1_image(image, registry, log, insecure=False, dockercfg_path=None):
    registry_session = RegistrySession(registry, insecure=insecure, dockercfg_path=dockercfg_path)

    headers = {'Accept': MEDIA_TYPE_DOCKER_V1}
    url = '/v1/repositories/{0}/tags/{1}'.format(image.get_repo(), image.tag)
    log.debug("verify_v1_image: querying {0}, headers: {1}".format(url, headers))

    response = registry_session.get(url, headers=headers)
    for r in chain(response.history, [response]):
        log.debug("verify_v1_image: [%s] %s", r.status_code, r.url)

    log.debug("verify_v1_image: response headers: %s", response.headers)
    response.raise_for_status()

    # if we returned ok, then everything is fine.
    return True


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
        if self.workflow.push_conf.pulp_registries:
            self.log.info("pulp registry configure, verify_media_types should not run")
            return
        image = self.workflow.tag_conf.unique_images[0]

        media_types = set()
        registries = deepcopy(get_registries(self.workflow, {}))
        for registry_name, registry in registries.items():
            initial_media_types = registry.get('expected_media_types', [])
            if not initial_media_types:
                continue

            expected_media_types = self.set_manifest_list_expectations(initial_media_types)

            pullspec = image.copy()
            pullspec.registry = registry_name
            insecure = registry.get('insecure', False)
            secret = registry.get('secret', None)

            check_digests = (MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST in expected_media_types or
                             MEDIA_TYPE_DOCKER_V2_SCHEMA2 in expected_media_types or
                             MEDIA_TYPE_DOCKER_V2_SCHEMA1 in expected_media_types)
            if check_digests:
                digests = get_manifest_digests(pullspec, registry_name, insecure, secret,
                                               require_digest=False)
                if digests:
                    if digests.v2_list:
                        self.log.info("Manifest list found")
                        if MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST in expected_media_types:
                            media_types.add(MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST)
                    if digests.v2:
                        self.log.info("V2 schema 2 digest found")
                        if MEDIA_TYPE_DOCKER_V2_SCHEMA2 in expected_media_types:
                            media_types.add(MEDIA_TYPE_DOCKER_V2_SCHEMA2)
                    if digests.v1:
                        self.log.info("V2 schema 1 digest found")
                        if MEDIA_TYPE_DOCKER_V2_SCHEMA1 in expected_media_types:
                            media_types.add(MEDIA_TYPE_DOCKER_V2_SCHEMA1)

            if MEDIA_TYPE_DOCKER_V1 in expected_media_types:
                if verify_v1_image(pullspec, registry_name, self.log, insecure, secret):
                    media_types.add(MEDIA_TYPE_DOCKER_V1)

            # sorting the media type here so the failure message is predictable for unit tests
            missing_types = []
            for media_type in sorted(expected_media_types):
                if media_type not in media_types:
                    missing_types.append(media_type)
            if missing_types:
                raise KeyError("expected media types {0} ".format(missing_types) +
                               "not in available media types {0}".format(sorted(media_types)))
        return sorted(media_types)

    def set_manifest_list_expectations(self, expected_media_types):
        if not self.workflow.postbuild_results.get(PLUGIN_GROUP_MANIFESTS_KEY):
            self.log.debug('Cannot check if only manifest list digest should be returned '
                           'because group manifests plugin did not run')
            return expected_media_types

        platforms = get_platforms(self.workflow)
        if not platforms:
            self.log.debug('Cannot check if only manifest list digest should be returned '
                           'because we have no platforms list')
            return expected_media_types

        try:
            platform_to_goarch = get_platform_to_goarch_mapping(self.workflow)
        except KeyError:
            self.log.debug('Cannot check if only manifest list digest should be returned '
                           'because there are no platform descriptors')
            return expected_media_types

        for plat in platforms:
            if platform_to_goarch[plat] == 'amd64':
                self.log.debug('amd64 was built, all media types available')
                return expected_media_types

        self.log.debug('amd64 was not built, only manifest list digest is available')
        return [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]
