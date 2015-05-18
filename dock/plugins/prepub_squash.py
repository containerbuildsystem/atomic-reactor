from dock.plugin import PrePublishPlugin
from docker_scripts.squash import Squash

__all__ = ('PrePublishSquashPlugin', )


class PrePublishSquashPlugin(PrePublishPlugin):

    """
    This feature requires docker-scripts package to be installed in version 0.3.2
    or higher.

    Usage:

    A json build config file should be created with following content:

    ```
      "prepublish_plugins": [{
        "name": "squash",
          "args": {
            "tag": "SQUASH_TAG",
            "from_layer": "FROM_LAYER"
          }
        }
      }
    ```

    The `tag` argument specifes the tag under which the new squashed image will
    be registered. The `from_layer` argument specifies from which layer we want
    to squash.

    Of course it's possible to override it at runtime, like this: `--substitute prepublish_plugins.squash.tag=image:squashed
      --substitute prepublish_plugins.squash.from_layer=asdasd2332`.
    """

    key = "squash"
    # Fail the build in case of squashing error
    can_fail = False

    def __init__(self, tasker, workflow, tag=None, from_layer=None):
        """
        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param from_layer: The layer from we will squash - by default it'll be the first layer
        :param tag: str, new name of the image - by default use the former one
        """
        super(PrePublishSquashPlugin, self).__init__(tasker, workflow)
        self.image = self.workflow.builder.image_id
        self.from_layer = from_layer
        self.tag = tag or str(self.workflow.builder.image)

    def run(self):
        new_id = Squash(log=self.log, image=self.image,
                        from_layer=self.from_layer, tag=self.tag).run()
        self.workflow.builder.image_id = new_id
        self.tasker.remove_image(self.image)
