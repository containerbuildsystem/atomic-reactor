"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Check parent image(s) the build will use, enforcing that they can only come from
the specified registry. It also validates platforms in the parent images and stores
their manifest digests.
"""
from io import BytesIO
from typing import Optional, Union

import requests
from osbs.utils import ImageName
from requests.exceptions import HTTPError, RetryError, Timeout

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import (get_platforms, base_image_is_custom, base_image_is_scratch,
                                 get_checksums, get_manifest_media_type,
                                 RegistrySession, RegistryClient)


class CheckBaseImagePlugin(PreBuildPlugin):
    key = "check_base_image"
    is_allowed_to_fail = False

    def __init__(self, workflow):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        """
        super(CheckBaseImagePlugin, self).__init__(workflow)

        self.source_registry_docker_uri = self.workflow.conf.source_registry['uri'].docker_uri

        self.allowed_registries = [reg['uri'].docker_uri for reg in workflow.conf.pull_registries]
        self.allowed_registries.append(self.source_registry_docker_uri)

        self.manifest_list_cache = {}
        # RegistryClient instances cached by registry name
        self.registry_clients = {}

    def run(self):
        """
        Check parent images to ensure they only come from allowed registries.
        """
        self.manifest_list_cache.clear()

        digest_fetching_exceptions = []
        for parent in self.workflow.data.dockerfile_images.keys():
            if base_image_is_custom(parent.to_str()) or base_image_is_scratch(parent.to_str()):
                continue

            image = parent
            use_original_tag = False
            # base_image_key is an ImageName, so compare parent as an ImageName also
            if image == self.workflow.data.dockerfile_images.base_image_key:
                use_original_tag = True
                image = self._resolve_base_image()

            self._ensure_image_registry(image)

            self._validate_platforms_in_image(image)
            try:
                self._store_manifest_digest(image, use_original_tag=use_original_tag)
            except RuntimeError as exc:
                digest_fetching_exceptions.append(exc)

            image_with_digest = self._get_image_with_digest(image)
            if image_with_digest is None:
                raise RuntimeError("Cannot resolve manifest digest for image {}".format(image))

            self.log.info("Replacing image '%s' with '%s'", image, image_with_digest)

            self.workflow.data.dockerfile_images[parent] = image_with_digest

        if digest_fetching_exceptions:
            raise RuntimeError('Error when extracting parent images manifest digests: {}'
                               .format(digest_fetching_exceptions))

    def _get_image_with_digest(self, image: ImageName) -> Optional[ImageName]:
        image_str = image.to_str()
        try:
            image_metadata = self.workflow.data.parent_images_digests[image_str]
        except KeyError:
            return None

        v2_list_type = get_manifest_media_type('v2_list')
        v2_type = get_manifest_media_type('v2')
        raw_digest = image_metadata.get(v2_list_type) or image_metadata.get(v2_type)
        if not raw_digest:
            return None

        digest = raw_digest.split(':', 1)[1]
        image_name = image.to_str(tag=False)
        new_image = '{}@sha256:{}'.format(image_name, digest)
        return ImageName.parse(new_image)

    def _store_manifest_digest(self, image: ImageName, use_original_tag: bool) -> None:
        """Store media type and digest for manifest list or v2 schema 2 manifest digest"""
        image_str = image.to_str()
        manifest_list = self._get_manifest_list(image)
        reg_client = self._get_registry_client(image.registry)
        if manifest_list:
            digest_dict = get_checksums(BytesIO(manifest_list.content), ['sha256'])
            media_type = get_manifest_media_type('v2_list')
        else:
            digests_dict = reg_client.get_all_manifests(image, versions=('v2',))
            media_type = get_manifest_media_type('v2')
            try:
                manifest_digest_response = digests_dict['v2']
            except KeyError as exc:
                raise RuntimeError(
                    'Unable to fetch manifest list or '
                    'v2 schema 2 digest for {} (Does image exist?)'.format(image_str)
                ) from exc

            digest_dict = get_checksums(BytesIO(manifest_digest_response.content), ['sha256'])

        manifest_digest = 'sha256:{}'.format(digest_dict['sha256sum'])
        parent_digests = {media_type: manifest_digest}
        if use_original_tag:
            # image tag may have been replaced with a ref for autorebuild; use original tag
            # to simplify fetching parent_images_digests data in other plugins
            image = image.copy()
            image.tag = self.workflow.data.dockerfile_images.base_image_key.tag
            image_str = image.to_str()

        self.workflow.data.parent_images_digests[image_str] = parent_digests

    def _resolve_base_image(self) -> Union[str, ImageName]:
        base_image = self.workflow.data.dockerfile_images.base_image
        self.log.info("using %s as base image.", base_image)
        return base_image

    def _ensure_image_registry(self, image: ImageName) -> None:
        """If plugin configured with a parent registry, ensure the image uses it"""
        # if registry specified in Dockerfile image, ensure it's the one allowed by config
        if image.registry:
            if image.registry not in self.allowed_registries:
                error = (
                        "Registry specified in dockerfile image doesn't match allowed registries. "
                        "Dockerfile: '%s'; allowed registries: '%s'"
                        % (image, self.allowed_registries))
                self.log.error("%s", error)
                raise RuntimeError(error)
        else:
            raise RuntimeError("Shouldn't happen, images should have already "
                               "registry set in dockerfile_images")

    def _get_manifest_list(self, image: ImageName) -> requests.Response:
        """try to figure out manifest list"""
        if image in self.manifest_list_cache:
            return self.manifest_list_cache[image]

        reg_client = self._get_registry_client(image.registry)

        manifest_list = reg_client.get_manifest_list(image)
        if '@sha256:' in str(image) and not manifest_list:
            # we want to adjust the tag only for manifest list fetching
            image = image.copy()

            try:
                config_blob = reg_client.get_config_from_registry(image, image.tag)
            except (HTTPError, RetryError, Timeout) as ex:
                self.log.warning('Unable to fetch config for %s, got error %s',
                                 image, ex.response.status_code)
                raise RuntimeError('Unable to fetch config for base image') from ex

            release = config_blob['config']['Labels']['release']
            version = config_blob['config']['Labels']['version']
            docker_tag = "%s-%s" % (version, release)
            image.tag = docker_tag

            manifest_list = reg_client.get_manifest_list(image)
        self.manifest_list_cache[image] = manifest_list
        return self.manifest_list_cache[image]

    def _validate_platforms_in_image(self, image: ImageName) -> None:
        """Ensure that the image provides all platforms expected for the build."""
        expected_platforms = get_platforms(self.workflow)
        if not expected_platforms:
            self.log.info('Skipping validation of available platforms '
                          'because expected platforms are unknown')
            return

        platform_to_arch = self.workflow.conf.platform_to_goarch_mapping

        manifest_list = self._get_manifest_list(image)

        if not manifest_list:
            if len(expected_platforms) == 1:
                self.log.warning('Skipping validation of available platforms for base image: '
                                 'this is a single platform build and base image has no manifest '
                                 'list')
                return
            else:
                raise RuntimeError('Unable to fetch manifest list for base image {}'.format(image))

        all_manifests = manifest_list.json()['manifests']
        manifest_list_arches = set(
            manifest['platform']['architecture'] for manifest in all_manifests)

        expected_arches = set(
            platform_to_arch[platform] for platform in expected_platforms)

        self.log.info('Manifest list arches: %s, expected arches: %s',
                      manifest_list_arches, expected_arches)

        missing_arches = expected_arches - manifest_list_arches
        if missing_arches:
            arches_str = ', '.join(sorted(missing_arches))
            raise RuntimeError('Base image {} not available for arches: {}'
                               .format(image, arches_str))

        self.log.info('Base image is a manifest list for all required platforms')

    def _get_registry_client(self, registry: str) -> RegistryClient:
        """
        Get registry client for specified registry, cached by registry name
        """
        client = self.registry_clients.get(registry)
        if client is None:
            session = RegistrySession.create_from_config(self.workflow.conf, registry=registry)
            client = RegistryClient(session)
            self.registry_clients[registry] = client
        return client
