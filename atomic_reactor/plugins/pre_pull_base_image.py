"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pull base image to build our layer on.
"""

from __future__ import unicode_literals

from atomic_reactor.plugin import PreBuildPlugin

class PullBaseImagePlugin(PreBuildPlugin):
    key = "pull_base_image"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, parent_registry=None, parent_registry_insecure=False):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param parent_registry: registry to enforce pulling from
        :param parent_registry_insecure: allow connecting to the registry over plain http
        """
        # call parent constructor
        super(PullBaseImagePlugin, self).__init__(tasker, workflow)

        self.parent_registry = parent_registry
        self.parent_registry_insecure = parent_registry_insecure

    def run(self):
        """
        pull base image
        """
        base_image = self.workflow.builder.base_image
        if self.parent_registry is not None:
            self.log.info("pulling base image '%s' from registry '%s'",
                          base_image, self.parent_registry)
        else:
            self.log.info("pulling base image '%s'", base_image)

        base_image_with_registry = base_image.copy()

        if self.parent_registry:
            # registry in dockerfile doesn't match provided source registry
            if base_image.registry and base_image.registry != self.parent_registry:
                self.log.error("registry in dockerfile doesn't match provided source registry, "
                               "dockerfile = '%s', provided = '%s'",
                               base_image.registry, self.parent_registry)
                raise RuntimeError(
                    "Registry specified in dockerfile doesn't match provided one. "
                    "Dockerfile: '%s', Provided: '%s'"
                    % (base_image.registry, self.parent_registry))

            base_image_with_registry.registry = self.parent_registry

        pulled_base = self.tasker.pull_image(base_image_with_registry,
                                             insecure=self.parent_registry_insecure)
        if (base_image_with_registry.namespace != 'library' and
                not self.tasker.image_exists(base_image_with_registry.to_str())):
            self.log.info("'%s' not found", base_image_with_registry.to_str())
            base_image_with_registry.namespace = 'library'
            self.log.info("trying '%s'", base_image_with_registry.to_str())
            pulled_base = self.tasker.pull_image(base_image_with_registry,
                                                 insecure=self.parent_registry_insecure)

        self.workflow.pulled_base_images.add(pulled_base)

        if not base_image.registry:
            response = self.tasker.tag_image(base_image_with_registry, base_image, force=True)
            self.workflow.pulled_base_images.add(response)
            pulled_base = response

        self.log.debug("image '%s' is available", pulled_base)
