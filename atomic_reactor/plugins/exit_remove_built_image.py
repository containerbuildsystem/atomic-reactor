"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Remove built image (this only makes sense if you store the image in some registry first)
"""
from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.utils import imageutil

__all__ = ('GarbageCollectionPlugin', )


def defer_removal(workflow, image):
    key = GarbageCollectionPlugin.key
    workflow.data.plugin_workspace.setdefault(key, {})
    workspace = workflow.data.plugin_workspace[key]
    workspace.setdefault('images_to_remove', set())
    workspace['images_to_remove'].add(image)


class GarbageCollectionPlugin(ExitPlugin):
    key = "remove_built_image"

    def __init__(self, workflow, remove_pulled_base_image=True):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :param remove_pulled_base_image: bool, remove also base image? default=True
        """
        # call parent constructor
        super(GarbageCollectionPlugin, self).__init__(workflow)
        self.remove_base_image = remove_pulled_base_image

    def run(self):
        # OSBS2 TBD
        image = self.workflow.data.image_id
        if image:
            self.remove_image(image, force=True)

        if self.remove_base_image and self.workflow.data.pulled_base_images:
            for base_image_tag in self.workflow.data.pulled_base_images:
                self.remove_image(base_image_tag, force=False)

        workspace = self.workflow.data.plugin_workspace.get(self.key, {})
        images_to_remove = workspace.get('images_to_remove', [])
        for image in images_to_remove:
            self.remove_image(image, force=True)

    def remove_image(self, image, force=False):
        try:
            # OSBS2 TBD
            imageutil.remove_image(image, force=force)
        except Exception as ex:
            self.log.warning("exception while removing image %s: %r, ignoring",
                             image, ex)
