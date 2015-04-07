"""
Remove built image (this only makes sense if you store the image in some registry first)
"""
from dock.plugin import PostBuildPlugin


__all__ = ('GarbageCollectionPlugin', )


class GarbageCollectionPlugin(PostBuildPlugin):
    key = "remove_built_image"
    can_fail = True

    def __init__(self, tasker, workflow):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        """
        # call parent constructor
        super(GarbageCollectionPlugin, self).__init__(tasker, workflow)

    def run(self):
        image = self.workflow.builder.image_id
        if not image:
            self.log.error("no built image, nothing to remove")
            return
        self.tasker.remove_image(image, force=True)
