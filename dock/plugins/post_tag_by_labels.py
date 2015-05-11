"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from dock.plugin import PostBuildPlugin


__all__ = ('TagByLabelsPlugin', )


class TagByLabelsPlugin(PostBuildPlugin):
    key = "tag_by_labels"
    can_fail = False

    def __init__(self, tasker, workflow, registry_uri, insecure=False):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param registry_uri: str, registry URI where the image should be pushed
        :param insecure: bool, allow connection to registry to be insecure
        """
        # call parent constructor
        super(TagByLabelsPlugin, self).__init__(tasker, workflow)
        self.registry_uri = registry_uri
        self.insecure = insecure

    def run(self):
        if not self.workflow.built_image_inspect:
            raise RuntimeError("There are no inspect data of built image. "
                               "Have the build succeeded?")
        if "Labels" not in self.workflow.built_image_inspect["ContainerConfig"]:
            raise RuntimeError("No labels specified.")

        def get_label(label_name):
            try:
                return self.workflow.built_image_inspect["ContainerConfig"]["Labels"][label_name]
            except KeyError:
                raise RuntimeError("Missing label '%s'.", label_name)

        name = get_label("Name")
        version = get_label("Version")
        release = get_label("Release")

        image = "%s:%s_%s" % (name, version, release)

        target_registries_insecure = self.insecure or self.workflow.target_registries_insecure

        self.workflow.tag_and_push_conf.add_image(self.registry_uri, image, target_registries_insecure)
