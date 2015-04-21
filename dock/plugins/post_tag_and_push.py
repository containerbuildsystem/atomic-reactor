from dock.plugin import PostBuildPlugin
from dock.util import ImageName


__all__ = ('TagAndPushPlugin', )


class TagAndPushPlugin(PostBuildPlugin):
    key = "tag_and_push"
    can_fail = False

    def __init__(self, tasker, workflow, mapping=None, insecure=False):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param mapping: dict, map with following structure (insecure in
                              mapping overrides global insecure):
          {
            "<registry_uri>": {
              "insecure": false,
              "image_names": [
                "image-name1",
                "prefix/image-name2",
              ],
            }
            "...": {...}
          }
        :param insecure: bool, allow connection to registry to be insecure
        """
        # call parent constructor
        super(TagAndPushPlugin, self).__init__(tasker, workflow)
        self.mapping = mapping
        self.insecure = insecure

    def run(self):
        pushed_images = []
        self.workflow.tag_and_push_conf.merge_with_mapping(self.mapping)
        for registry_uri in self.workflow.tag_and_push_conf.registries:
            registry_conf = self.workflow.tag_and_push_conf[registry_uri]
            insecure = registry_conf.get("insecure", self.insecure)
            try:
                image_names = registry_conf['image_names']
            except KeyError:
                self.log.error("Registry '%s' doesn't have any image names, skipping...", registry_uri)
                continue
            for image in image_names:
                image_name = ImageName.parse(image)
                if image_name.registry:
                    assert image_name.registry == registry_uri
                image_name.registry = registry_uri
                self.tasker.tag_and_push_image(self.workflow.builder.image_id, image_name,
                                               insecure=insecure)
                pushed_images.append(image_name.to_str())
        return pushed_images
