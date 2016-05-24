"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Remove built image (this only makes sense if you store the image in some registry first)
"""
from atomic_reactor.plugin import ExitPlugin

from docker.errors import APIError

__all__ = ('GarbageCollectionPlugin', )


def defer_removal(workflow, image):
    key = GarbageCollectionPlugin.key
    workflow.plugin_workspace.setdefault(key, {})
    workspace = workflow.plugin_workspace[key]
    workspace.setdefault('images_to_remove', set())
    workspace['images_to_remove'].add(image)


class GarbageCollectionPlugin(ExitPlugin):
    key = "remove_built_image"

    def __init__(self, tasker, workflow, remove_pulled_base_image=True):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param remove_pulled_base_image: bool, remove also base image? default=True
        """
        # call parent constructor
        super(GarbageCollectionPlugin, self).__init__(tasker, workflow)
        self.remove_base_image = remove_pulled_base_image

    def run(self):
        image = self.workflow.builder.image_id
        if image:
            self.remove_image(image, force=True)
        else:
            self.log.error("no built image, nothing to remove")

        if self.remove_base_image and self.workflow.pulled_base_images:
            # FIXME: we may need to add force here, let's try it like this for now
            # FIXME: when ID of pulled img matches an ID of an image already present, don't remove
            for base_image_tag in self.workflow.pulled_base_images:
                self.remove_image(base_image_tag, force=False)

        workspace = self.workflow.plugin_workspace.get(self.key, {})
        images_to_remove = workspace.get('images_to_remove', [])
        for image in images_to_remove:
            self.remove_image(image, force=True)

    def remove_image(self, image, force=False):
        try:
            self.tasker.remove_image(image, force=force)
        except APIError as ex:
            if ex.is_client_error():
                self.log.warning("failed to remove image %s (%s: %s), ignoring",
                                 image, ex.response.status_code, ex.response.reason)
            else:
                raise
        except Exception as ex:
            self.log.warning("exception while removing image %s: %r, ignoring",
                             image, ex)
