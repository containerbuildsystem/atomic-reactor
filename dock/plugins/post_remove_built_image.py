"""
Remove built image (this only makes sense if you store the image in some registry first)
"""
from dock.plugin import PostBuildPlugin


__all__ = ('GarbageCollectionPlugin', )


class GarbageCollectionPlugin(PostBuildPlugin):
    key = "remove_built_image"

    def __init__(self, tasker, workflow):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        """
        # call parent constructor
        super(GarbageCollectionPlugin, self).__init__(tasker, workflow)

    def run(self):
        self.tasker.remove_image(self.workflow.build.image_id, force=True)
