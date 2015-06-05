"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from dock.plugin import PostBuildPlugin


__all__ = ('TagAndPushPlugin', )


class TagAndPushPlugin(PostBuildPlugin):
    key = "tag_and_push"
    can_fail = False

    def __init__(self, tasker, workflow, **kwargs):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        """
        # call parent constructor
        super(TagAndPushPlugin, self).__init__(tasker, workflow)
        self.log.warning("Ignoring arguments %s", kwargs)

    def run(self):
        pushed_images = []
        for registry in self.workflow.push_conf.all_docker_registries:
            for image in self.workflow.tag_conf.images:
                registry_image = image.copy()
                registry_image.registry = registry.uri
                self.tasker.tag_and_push_image(self.workflow.builder.image_id, registry_image,
                                               insecure=registry.insecure, force=True)
                pushed_images.append(registry_image.to_str())
        return pushed_images
