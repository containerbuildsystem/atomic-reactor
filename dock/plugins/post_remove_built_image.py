"""
Remove built image (this only makes sense if you store the image in some registry first)
"""
from dock.plugin import PostBuildPlugin
from dock.util import ImageName


__all__ = ('GarbageCollectionPlugin', )


class GarbageCollectionPlugin(PostBuildPlugin):
    key = "remove_built_image"
    can_fail = True

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
        if not image:
            self.log.error("no built image, nothing to remove")
            return
        self.tasker.remove_image(image, force=True)
        if self.remove_base_image and self.workflow.pulled_base_image:
            # FIXME: we may need to add force here, let's try it like this for now
            # FIXME: when ID of pulled img matches an ID of an image already present, don't remove
            self.tasker.remove_image(ImageName.parse(self.workflow.pulled_base_image))
