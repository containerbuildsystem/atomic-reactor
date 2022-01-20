"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.constants import PLUGIN_PUSH_FLOATING_TAGS_KEY, PLUGIN_GROUP_MANIFESTS_KEY
from atomic_reactor.utils.manifest import ManifestUtil
from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.util import get_floating_images, get_unique_images


class PushFloatingTagsPlugin(ExitPlugin):
    """
    Push floating tags to registry
    """

    key = PLUGIN_PUSH_FLOATING_TAGS_KEY
    is_allowed_to_fail = False

    def __init__(self, workflow):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        """
        super(PushFloatingTagsPlugin, self).__init__(workflow)
        self.manifest_util = ManifestUtil(workflow, None, self.log)

    def add_floating_tags(self, session, manifest_list_data, floating_images):
        list_type = manifest_list_data.get("media_type")
        manifest = manifest_list_data.get("manifest")
        manifest_digest = manifest_list_data.get("manifest_digest")

        for image in floating_images:
            target_repo = image.to_str(registry=False, tag=False)
            # We have to call store_manifest_in_repository directly for each
            # referenced manifest, since each one should be a new tag that requires uploading
            # the manifest again
            self.log.debug("storing %s as %s", target_repo, image.tag)
            self.manifest_util.store_manifest_in_repository(session, manifest, list_type,
                                                            target_repo, target_repo, ref=image.tag)
        # And store the manifest list in the push_conf
        push_conf_registry = self.workflow.data.push_conf.add_docker_registry(
            session.registry, insecure=session.insecure
        )
        for image in floating_images:
            push_conf_registry.digests[image.tag] = manifest_digest
        registry_image = get_unique_images(self.workflow)[0]

        return registry_image.get_repo(explicit_namespace=False), manifest_digest

    def run(self):
        """
        Run the plugin.
        """
        if self.workflow.build_process_failed:
            self.log.info('Build failed, skipping %s', PLUGIN_PUSH_FLOATING_TAGS_KEY)
            return

        floating_tags = get_floating_images(self.workflow)
        if not floating_tags:
            self.log.info('No floating images to tag, skipping %s', PLUGIN_PUSH_FLOATING_TAGS_KEY)
            return

        #  can't run in the worker build
        if not self.workflow.is_orchestrator_build():
            self.log.warning('%s cannot be used by a worker builder', PLUGIN_PUSH_FLOATING_TAGS_KEY)
            return

        manifest_data = self.workflow.data.postbuild_results.get(PLUGIN_GROUP_MANIFESTS_KEY)
        if not manifest_data or not manifest_data.get("manifest_digest"):
            self.log.info('No manifest digest available, skipping %s',
                          PLUGIN_PUSH_FLOATING_TAGS_KEY)
            return

        digests = dict()

        for registry in self.manifest_util.registries:
            session = self.manifest_util.get_registry_session(registry)
            repo, digest = self.add_floating_tags(session, manifest_data, floating_tags)
            digests[repo] = digest
        return digests
