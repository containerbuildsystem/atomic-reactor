"""
Copyright (c) 2017-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

get the image manifest lists from the worker builders. If possible, group them together
and return them. if not, return empty dict after re-uploading it for all existing image
tags.
"""
from typing import Dict, List, NamedTuple, Union

from osbs.utils import ImageName

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.util import (
    ManifestDigest,
    RegistryClient,
    RegistrySession,
    get_manifest_media_type,
    get_primary_images,
    get_unique_images,
    get_platforms,
)
from atomic_reactor.utils.manifest import ManifestUtil
from atomic_reactor.constants import PLUGIN_GROUP_MANIFESTS_KEY, MEDIA_TYPE_OCI_V1_INDEX

# The plugin requires that the worker builds have already pushed their images into
# each registry that we want the final tags to end up in. There is code here to
# copy images between repositories in a single registry (which is simple, because
# it can be done entirely server-side), but not between registries. Extending the
# code to copy registries is possible, but would be more involved because of the
# size of layers and the complications of the protocol for copying them.


class BuiltImage(NamedTuple):
    """Represents a per-arch image which was built and pushed to a registry by the build task.

    pullspec: the full pullspec of the image
    platform: the platform this image was built for
    manifest_digest: the manifest digest of this image, uniquely identifies this image
    manifest_version: the manifest type of this image, should be 'v2' or 'oci'
        (a Docker v2 schema 2 manifest or an OCI manifest)
    """

    pullspec: ImageName
    platform: str
    manifest_digest: str
    manifest_version: str

    @property
    def repository(self) -> str:
        """Get the "{namespace}/{repo}" from the pullspec of this image."""
        return self.pullspec.to_str(registry=False, tag=False)


class GroupManifestsPlugin(PostBuildPlugin):
    is_allowed_to_fail = False
    key = PLUGIN_GROUP_MANIFESTS_KEY

    def __init__(self, workflow):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        """
        # call parent constructor
        super(GroupManifestsPlugin, self).__init__(workflow)

        self.group = self.workflow.conf.group_manifests
        self.goarch = self.workflow.conf.platform_to_goarch_mapping

        self.manifest_util = ManifestUtil(self.workflow, self.log)
        self.non_floating_images = None

    def get_built_images(self, session: RegistrySession) -> List[BuiltImage]:
        """Get information about all the per-arch images that were built by the build tasks."""
        tag_conf = self.workflow.data.tag_conf
        client = RegistryClient(session)

        built_images = []

        for platform in get_platforms(self.workflow.data):
            # At this point, only the unique image has been built and pushed. Primary tags will
            #   be pushed by this plugin, floating tags by the push_floating_tags plugin.
            image = tag_conf.get_unique_images_with_platform(platform)[0]
            manifest_digests = client.get_manifest_digests(image, versions=("v2", "oci"))

            if len(manifest_digests) != 1:
                raise RuntimeError(
                    f"Expected to find a single manifest digest for {image}, "
                    f"but found multiple: {manifest_digests}"
                )

            manifest_version, manifest_digest = manifest_digests.popitem()
            built_images.append(BuiltImage(image, platform, manifest_digest, manifest_version))

        return built_images

    def group_manifests_and_tag(
        self, session: RegistrySession, built_images: List[BuiltImage]
    ) -> Dict[str, Union[str, ManifestDigest]]:
        """
        Creates a manifest list or OCI image index that groups the different manifests
        in built_images, then tags the result with all the configured tags found
        in workflow.data.tag_conf.
        """
        self.log.info("%s: Creating manifest list", session.registry)

        # Extract information about the manifests that we will group - we get the
        # size and content type of the manifest by querying the registry
        manifests = []
        for built_image in built_images:
            repository = built_image.repository
            manifest_digest = built_image.manifest_digest
            media_type = get_manifest_media_type(built_image.manifest_version)

            if media_type not in self.manifest_util.manifest_media_types:
                continue
            content, _, media_type, size = self.manifest_util.get_manifest(
                session, repository, manifest_digest
            )

            manifests.append({
                'content': content,
                'repository': repository,
                'digest': manifest_digest,
                'size': size,
                'media_type': media_type,
                'architecture': self.goarch[built_image.platform],
            })

        list_type, list_json = self.manifest_util.build_list(manifests)
        self.log.info("%s: Created manifest, Content-Type=%s\n%s", session.registry,
                      list_type, list_json)

        # Now push the manifest list to the registry once per each tag
        self.log.info("%s: Tagging manifest list", session.registry)

        for image in self.non_floating_images:
            target_repo = image.to_str(registry=False, tag=False)
            # We have to call store_manifest_in_repository directly for each
            # referenced manifest, since they potentially come from different repos
            for manifest in manifests:
                self.manifest_util.store_manifest_in_repository(session,
                                                                manifest['content'],
                                                                manifest['media_type'],
                                                                manifest['repository'],
                                                                target_repo,
                                                                ref=manifest['digest'])
            self.manifest_util.store_manifest_in_repository(session, list_json, list_type,
                                                            target_repo, target_repo, ref=image.tag)
        # Get the digest of the manifest list using one of the tags
        registry_image = get_unique_images(self.workflow)[0]
        _, digest_str, _, _ = self.manifest_util.get_manifest(session,
                                                              registry_image.to_str(registry=False,
                                                                                    tag=False),
                                                              registry_image.tag)

        if list_type == MEDIA_TYPE_OCI_V1_INDEX:
            digest = ManifestDigest(oci_index=digest_str)
        else:
            digest = ManifestDigest(v2_list=digest_str)

        tags = []
        for image in self.non_floating_images:
            tags.append(image.tag)

        self.log.info("%s: Manifest list digest is %s", session.registry, digest_str)
        self.log.debug("tags: %s digest: %s", tags, digest)

        return {'manifest': list_json, 'media_type': list_type, 'manifest_digest': digest}

    def tag_manifest_into_registry(self, session, source_digest: str, source_repo, images):
        manifest, media, digest = self.manifest_util.tag_manifest_into_registry(session,
                                                                                source_digest,
                                                                                source_repo,
                                                                                images)
        return {
            'manifest': manifest.decode('utf-8'),
            'media_type': media,
            'manifest_digest': digest,
        }

    def run(self):
        primary_images = get_primary_images(self.workflow)
        unique_images = get_unique_images(self.workflow)
        self.non_floating_images = primary_images + unique_images

        session = self.manifest_util.get_registry_session()
        built_images = self.get_built_images(session)

        if self.group:
            return self.group_manifests_and_tag(session, built_images)
        else:
            if len(built_images) != 1:
                raise RuntimeError('Without grouping only one built image is expected')
            built_image = built_images[0]
            source_digest = built_image.manifest_digest
            source_repo = built_image.repository

            return self.tag_manifest_into_registry(session, source_digest, source_repo,
                                                   self.non_floating_images)
