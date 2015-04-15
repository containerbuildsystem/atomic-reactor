from dock.plugin import PostBuildPlugin
from dock.util import split_repo_img_name_tag, join_repo_img_name_tag


__all__ = ('TagAndPushPlugin', )


class TagAndPushPlugin(PostBuildPlugin):
    key = "tag_and_push"

    def __init__(self, tasker, workflow, mapping, insecure=False):
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
        for registry_uri, registry_conf in self.mapping.items():
            insecure = registry_conf.get("insecure", self.insecure)
            try:
                image_names = registry_conf['image_names']
            except KeyError:
                self.log.error("Registry '%s' doesn't have any image names, skipping...", registry_uri)
                continue
            for image in image_names:
                reg_uri, image_name, tag = split_repo_img_name_tag(image)
                if reg_uri:
                    assert reg_uri == registry_uri
                self.tasker.tag_and_push_image(self.workflow.builder.image_id, image_name, registry_uri, tag,
                                               insecure=insecure)
                pushed_images.append(join_repo_img_name_tag(registry_uri, image_name, tag))
        return pushed_images
