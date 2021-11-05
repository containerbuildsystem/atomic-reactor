"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pull parent image(s) the build will use, enforcing that they can only come from
the specified registry.
If this build is an auto-rebuild, use the base image from the image change
trigger instead of what is in the Dockerfile.
Tag each image to a unique name (the build name plus a nonce) to be used during
this build so that it isn't removed by other builds doing clean-up.
"""
from io import BytesIO
from typing import Optional, Union

import docker
import requests

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import (get_platforms, base_image_is_custom,
                                 get_checksums, get_manifest_media_type,
                                 RegistrySession, RegistryClient, map_to_user_params)
from atomic_reactor.utils import imageutil
from requests.exceptions import HTTPError, RetryError, Timeout
from osbs.utils import ImageName


class PullBaseImagePlugin(PreBuildPlugin):
    key = "pull_base_image"
    is_allowed_to_fail = False

    args_from_user_params = map_to_user_params("parent_images_digests")

    def __init__(self, workflow, check_platforms=False, inspect_only=False,
                 parent_images_digests=None):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :param check_platforms: validate parent images provide all platforms expected for the build
        :param inspect_only: bool, if set to True, base images will not be pulled
        :param parent_images_digests: dict, parent images manifest digests
        """
        # call parent constructor
        super(PullBaseImagePlugin, self).__init__(workflow)

        self.check_platforms = check_platforms
        self.inspect_only = inspect_only
        pull_registries = workflow.conf.pull_registries

        self.source_registry_docker_uri = self.workflow.conf.source_registry['uri'].docker_uri

        self.allowed_registries = [reg['uri'].docker_uri for reg in pull_registries]
        self.allowed_registries.append(self.source_registry_docker_uri)

        if parent_images_digests:
            # OSBS2 TBD
            metadata = self.workflow.parent_images_digests
            metadata.update(parent_images_digests)

        self.manifest_list_cache = {}
        # RegistryClient instances cached by registry name
        self.registry_clients = {}

    def run(self):
        """
        Pull parent images and retag them uniquely for this build.
        """
        self.manifest_list_cache.clear()

        digest_fetching_exceptions = []
        for nonce, parent in enumerate(self.workflow.dockerfile_images.keys()):
            if base_image_is_custom(parent.to_str()):
                continue

            image = parent
            use_original_tag = False
            # base_image_key is an ImageName, so compare parent as an ImageName also
            if image == self.workflow.dockerfile_images.base_image_key:
                use_original_tag = True
                image = self._resolve_base_image()

            self._ensure_image_registry(image)

            if self.check_platforms:
                # run only at orchestrator
                self._validate_platforms_in_image(image)
                try:
                    self._store_manifest_digest(image, use_original_tag=use_original_tag)
                except RuntimeError as exc:
                    digest_fetching_exceptions.append(exc)

            image_with_digest = self._get_image_with_digest(image)
            if image_with_digest is None:
                self.log.warning("Cannot resolve manifest digest for image '%s'", image)
            else:
                self.log.info("Replacing image '%s' with '%s'", image, image_with_digest)
                image = image_with_digest

            if not self.inspect_only:
                image = self._pull_and_tag_image(image, str(nonce))
            self.workflow.dockerfile_images[parent] = image

        if digest_fetching_exceptions:
            raise RuntimeError('Error when extracting parent images manifest digests: {}'
                               .format(digest_fetching_exceptions))

    def _get_image_with_digest(self, image: ImageName) -> Optional[ImageName]:
        image_str = image.to_str()
        try:
            image_metadata = self.workflow.parent_images_digests[image_str]
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
            image.tag = self.workflow.dockerfile_images.base_image_key.tag
            image_str = image.to_str()

        self.workflow.parent_images_digests[image_str] = parent_digests

    def _resolve_base_image(self) -> Union[str, ImageName]:
        base_image = self.workflow.dockerfile_images.base_image
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

    def _pull_and_tag_image(self, image: ImageName, nonce: str) -> ImageName:
        """Docker pull the image and tag it uniquely for use by this build"""
        image = image.copy()
#        reg_client = self._get_registry_client(image.registry)
        for _ in range(20):
            # retry until pull and tag is successful or definitively fails.
            # should never require 20 retries but there's a race condition at work.
            # just in case something goes wildly wrong, limit to 20 so it terminates.
            try:
                # remove pulling
                # OSBS2 TBD
                # self.tasker.pull_image(image, insecure=reg_client.insecure,
                #                        dockercfg_path=reg_client.dockercfg_path)
                self.workflow.pulled_base_images.add(image.to_str())
            except Exception:
                self.log.error('failed to pull image: %s', image)
                raise

            # Attempt to tag it using a unique ID. We might have to retry
            # if another build with the same parent image is finishing up
            # and removing images it pulled.

            # Use the Pipeline run name as the unique ID
            unique_id = self.workflow.user_params['pipeline_run_name']
            new_image = ImageName(repo=unique_id, tag=nonce)

            try:
                self.log.info("tagging pulled image")
                # OSBS2 TBD
                response = imageutil.tag_image(image, new_image)
                self.workflow.pulled_base_images.add(response)
                self.log.debug("image '%s' is available as '%s'", image, new_image)
                return new_image
            except docker.errors.NotFound:
                # If we get here, some other build raced us to remove
                # the parent image, and that build won.
                # Retry the pull immediately.
                self.log.info("re-pulling removed image")
                continue

        # Failed to tag it after 20 tries
        self.log.error("giving up trying to pull image")
        raise RuntimeError("too many attempts to pull and tag image")

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
            session = RegistrySession.create_from_config(self.workflow, registry=registry)
            client = RegistryClient(session)
            self.registry_clients[registry] = client
        return client
