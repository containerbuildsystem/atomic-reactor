"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from copy import deepcopy
import re

from atomic_reactor.plugin import PostBuildPlugin


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
                           parameters. Currently only the "insecure" optional boolean parameter
                           is supported which controls whether pushes are allowed over plain HTTP.
        """
        # call parent constructor
        super(TagAndPushPlugin, self).__init__(tasker, workflow)

        self.registries = deepcopy(registries)

    def run(self):
        pushed_images = []

        if not self.workflow.tag_conf.unique_images:
            self.workflow.tag_conf.add_unique_image(self.workflow.image)

        for registry, registry_conf in self.registries.items():
            insecure = registry_conf.get('insecure', False)
            push_conf_registry = \
                self.workflow.push_conf.add_docker_registry(registry, insecure=insecure)

            for image in self.workflow.tag_conf.images:
                if image.registry:
                    raise RuntimeError("Image name must not contain registry: %r" % image.registry)

                registry_image = image.copy()
                registry_image.registry = registry
                logs = self.tasker.tag_and_push_image(self.workflow.builder.image_id,
                                                      registry_image, insecure=insecure,
                                                      force=True)

                pushed_images.append(registry_image.to_str())

                digest = self.extract_digest(logs)
                if digest:
                    tag = registry_image.to_str(registry=False)
                    push_conf_registry.digests[tag] = digest

        return pushed_images

    @staticmethod
    def extract_digest(logs):
        for j in reversed(logs):
            if "status" not in j:
                continue

            m = re.match(r'^Digest: ([a-z0-9]+:[a-f0-9]+)$', j['status'])
            if m:
                return m.group(1)

        return None
