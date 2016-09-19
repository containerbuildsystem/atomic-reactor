"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.constants import INSPECT_CONFIG
from atomic_reactor.util import get_preferred_label


__all__ = ('TagByLabelsPlugin', )


class TagByLabelsPlugin(PostBuildPlugin):
    """
    Use labels Name, Version and Release of final image and create tags:
     * Name:Version
     * Name:Version-Release
    """
    key = "tag_by_labels"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, unique_tag_only=False, **kwargs):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param unique_tag_only: bool, when true image will only be tagged with
            unique tag, and not primary tags
        """
        # call parent constructor
        super(TagByLabelsPlugin, self).__init__(tasker, workflow)
        self.unique_tag_only = unique_tag_only
        if kwargs:
            self.log.warning("ignoring arguments %s", kwargs)

    def run(self):
        if not self.workflow.built_image_inspect:
            raise RuntimeError("There are no inspect data of built image. "
                               "Have the build succeeded?")
        if "Labels" not in self.workflow.built_image_inspect[INSPECT_CONFIG]:
            raise RuntimeError("No labels specified.")

        def get_label(label_name):
            try:
                return get_preferred_label(
                    self.workflow.built_image_inspect[INSPECT_CONFIG]['Labels'],
                    label_name)
            except KeyError:
                raise RuntimeError("Missing label '%s'." % label_name)

        name = get_label("name")

        unique_tag = self.workflow.builder.image.tag
        n_unique = "%s:%s" % (name, unique_tag)
        self.workflow.tag_conf.add_unique_image(n_unique)

        if self.unique_tag_only:
            self.log.debug('Skipping transient tags')
            return

        version = get_label("version")
        release = get_label("release")

        nvr = "%s:%s-%s" % (name, version, release)
        nv = "%s:%s" % (name, version)
        n = "%s:latest" % name

        self.workflow.tag_conf.add_primary_images([nvr, nv, n])
