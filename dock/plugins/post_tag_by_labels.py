from dock.plugin import PostBuildPlugin


__all__ = ('TagByLabelsPlugin', )


class TagByLabelsPlugin(PostBuildPlugin):
    key = "tag_by_labels"
    can_fail = False

    def __init__(self, tasker, workflow, registry, insecure=False):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param registry: str, registry URI where the image should be pushed
        :param insecure: bool, allow connection to registry to be insecure
        """
        # call parent constructor
        super(TagByLabelsPlugin, self).__init__(tasker, workflow)
        self.registry = registry
        self.insecure = insecure

    def run(self):
        name = self.workflow.built_image_inspect["ContainerConfig"]["Labels"]['Name']
        version = self.workflow.built_image_inspect["ContainerConfig"]["Labels"]['Version']
        release = self.workflow.built_image_inspect["ContainerConfig"]["Labels"]['Release']

        image = "%s:%s_%s" % (name, version, release)

        target_registries_insecure = self.insecure or self.workflow.target_registries_insecure

        self.workflow.tag_and_push_conf.add_image(self.registry, image, target_registries_insecure)
