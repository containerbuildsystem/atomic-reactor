"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from copy import deepcopy

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.plugins.exit_remove_built_image import defer_removal
from atomic_reactor.util import get_manifest_digests, get_config_from_registry


__all__ = ('TagAndPushPlugin', )


class TagAndPushPlugin(PostBuildPlugin):
    """
    Use tags from workflow.tag_conf and push the images to workflow.push_conf
    """

    key = "tag_and_push"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, registries):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param registries: dict, keys are docker registries, values are dicts containing per-registry
                           parameters.
                           Params:
                            * "insecure" optional boolean - controls whether pushes are allowed over
                              plain HTTP.
                            * "secret" optional string - path to the secret, which stores
                              email, login and password for remote registry
        """
        # call parent constructor
        super(TagAndPushPlugin, self).__init__(tasker, workflow)

        self.registries = deepcopy(registries)

    def run(self):
        pushed_images = []

        if not self.workflow.tag_conf.unique_images:
            self.workflow.tag_conf.add_unique_image(self.workflow.image)

        first_v2_digest = None
        first_registry_image = None
        for registry, registry_conf in self.registries.items():
            insecure = registry_conf.get('insecure', False)
            push_conf_registry = \
                self.workflow.push_conf.add_docker_registry(registry, insecure=insecure)

            docker_push_secret = registry_conf.get('secret', None)
            self.log.info("Registry %s secret %s", registry, docker_push_secret)

            for image in self.workflow.tag_conf.images:
                if image.registry:
                    raise RuntimeError("Image name must not contain registry: %r" % image.registry)

                registry_image = image.copy()
                registry_image.registry = registry
                logs = self.tasker.tag_and_push_image(self.workflow.builder.image_id,
                                                      registry_image, insecure=insecure,
                                                      force=True, dockercfg=docker_push_secret)

                pushed_images.append(registry_image)
                defer_removal(self.workflow, registry_image)

                digests = get_manifest_digests(registry_image, registry,
                                               insecure, docker_push_secret)
                tag = registry_image.to_str(registry=False)
                push_conf_registry.digests[tag] = digests

                if not first_v2_digest and digests.v2:
                    first_v2_digest = digests.v2
                    first_registry_image = registry_image

            if first_v2_digest:
                push_conf_registry.config = get_config_from_registry(
                    first_registry_image, registry, first_v2_digest, insecure,
                    docker_push_secret, 'v2')
            else:
                self.log.info("V2 schema 2 digest is not available")

        self.log.info("All images were tagged and pushed")
        return pushed_images
