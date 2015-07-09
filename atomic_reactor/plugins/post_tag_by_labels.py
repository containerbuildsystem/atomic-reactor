"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.plugin import PostBuildPlugin


__all__ = ('TagByLabelsPlugin', )


class TagByLabelsPlugin(PostBuildPlugin):
    """
    Use labels Name, Version and Release of final image and create tags:
     * Name:Version
     * Name:Version_Release
    """
    key = "tag_by_labels"
    can_fail = False

    def __init__(self, tasker, workflow, scratch_build=False, **kwargs):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param scratch_build: bool, whether we are tagging a scratch build
        """
        # call parent constructor
        super(TagByLabelsPlugin, self).__init__(tasker, workflow)
        self.scratch_build = scratch_build

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

        unique_tag = self.workflow.builder.image.tag

        nvr = "%s:%s_%s" % (name, version, release)
        nv = "%s:%s" % (name, version)
        n = "%s:latest" % name
        n_unique = "%s:%s" % (name, unique_tag)

        primary_tags = [nvr, nv, n]

        if self.scratch_build:
            primary_tags = map(lambda tag: 'scratch-' + tag, primary_tags)
            n_unique = 'scratch-' + n_unique

        self.workflow.tag_conf.add_primary_images(primary_tags)
        self.workflow.tag_conf.add_unique_image(n_unique)
