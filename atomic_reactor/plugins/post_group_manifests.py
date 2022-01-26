"""
Copyright (c) 2017, 2018, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

get the image manifest lists from the worker builders. If possible, group them together
and return them. if not, return empty dict after re-uploading it for all existing image
tags.
"""


from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.util import (ManifestDigest, get_manifest_media_type,
                                 get_primary_images, get_unique_images)
from atomic_reactor.utils.manifest import ManifestUtil
from atomic_reactor.constants import PLUGIN_GROUP_MANIFESTS_KEY, MEDIA_TYPE_OCI_V1_INDEX

# The plugin requires that the worker builds have already pushed their images into
# each registry that we want the final tags to end up in. There is code here to
# copy images between repositories in a single registry (which is simple, because
# it can be done entirely server-side), but not between registries. Extending the
# code to copy registries is possible, but would be more involved because of the
# size of layers and the complications of the protocol for copying them.


class GroupManifestsPlugin(PostBuildPlugin):
    is_allowed_to_fail = False
    key = PLUGIN_GROUP_MANIFESTS_KEY

    def __init__(self, workflow, registries=None):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :param registries: dict, keys are docker registries, values are dicts containing
                           per-registry parameters.
                           Params:
                            * "secret" optional string - path to the secret, which stores
                              login and password for remote registry
        """
        # call parent constructor
        super(GroupManifestsPlugin, self).__init__(workflow)

        self.group = self.workflow.conf.group_manifests
        self.goarch = self.workflow.conf.platform_to_goarch_mapping

        self.manifest_util = ManifestUtil(self.workflow, registries, self.log)
        self.non_floating_images = None

    def group_manifests_and_tag(self, session, worker_digests):
        """
        Creates a manifest list or OCI image index that groups the different manifests
        in worker_digests, then tags the result with with all the configured tags found
        in workflow.data.tag_conf.
        """
        self.log.info("%s: Creating manifest list", session.registry)

        # Extract information about the manifests that we will group - we get the
        # size and content type of the manifest by querying the registry
        manifests = []
        for platform, worker_image in worker_digests.items():
            repository = worker_image['repository']
            digest = worker_image['digest']
            media_type = get_manifest_media_type(worker_image['version'])
            if media_type not in self.manifest_util.manifest_media_types:
                continue
            content, _, media_type, size = self.manifest_util.get_manifest(session, repository,
                                                                           digest)

            manifests.append({
                'content': content,
                'repository': repository,
                'digest': digest,
                'size': size,
                'media_type': media_type,
                'architecture': self.goarch.get(platform, platform),
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

        # And store the manifest list in the push_conf
        push_conf_registry = self.workflow.data.push_conf.add_docker_registry(
            session.registry, insecure=session.insecure
        )
        tags = []
        for image in self.non_floating_images:
            push_conf_registry.digests[image.tag] = digest
            tags.append(image.tag)

        self.log.info("%s: Manifest list digest is %s", session.registry, digest_str)
        self.log.debug("tags: %s digest: %s", tags, digest)

        return {'manifest': list_json, 'media_type': list_type, 'manifest_digest': digest}

    def sort_annotations(self):
        """
        Return a map of maps to look up a single "worker digest" that has information
        about where to find an image manifest for each registry/architecture combination:

          worker_digest = <result>[registry][architecture]
        """

        all_annotations = self.workflow.data.build_result.annotations['worker-builds']
        all_platforms = set(all_annotations)
        if len(all_platforms) == 0:
            raise RuntimeError("No worker builds found, cannot group them")

        return self.manifest_util.sort_annotations(all_annotations)

    def tag_manifest_into_registry(self, session, source_digest: str, source_repo, images):
        manifest, media, digest = self.manifest_util.tag_manifest_into_registry(session,
                                                                                source_digest,
                                                                                source_repo,
                                                                                images)
        return {'manifest': manifest, 'media_type': media, 'manifest_digest': digest}

    def run(self):
        primary_images = get_primary_images(self.workflow)
        unique_images = get_unique_images(self.workflow)
        self.non_floating_images = primary_images + unique_images

        for registry, source in self.sort_annotations().items():
            session = self.manifest_util.get_registry_session(registry)

            if self.group:
                return self.group_manifests_and_tag(session, source)
            else:
                if len(source) != 1:
                    raise RuntimeError('Without grouping only one source is expected')
                # source.values() isn't a list and can't be indexed, so this clumsy workaround
                _, orig_digest = source.popitem()
                source_digest = orig_digest['digest']
                source_repo = orig_digest['repository']

                return self.tag_manifest_into_registry(session, source_digest, source_repo,
                                                       self.non_floating_images)
