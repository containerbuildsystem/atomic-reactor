"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pre-build plugin that changes the parent images used in FROM instructions
to the more specific names given by the builder.
"""
from atomic_reactor.plugin import PreBuildPlugin
from dockerfile_parse import DockerfileParser


class BaseImageMismatch(RuntimeError):
    pass


class ParentImageUnresolved(RuntimeError):
    pass


class ParentImageMissing(RuntimeError):
    pass


class NoIdInspection(RuntimeError):
    pass


class ChangeFromPlugin(PreBuildPlugin):
    key = "change_from_in_dockerfile"

    def __init__(self, tasker, workflow):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        """
        # call parent constructor
        super(ChangeFromPlugin, self).__init__(tasker, workflow)

    def run(self):
        builder = self.workflow.builder
        dfp = DockerfileParser(builder.df_path)

        df_base = dfp.baseimage
        build_base = builder.base_image.to_str()

        # do some sanity checks to defend against bugs and rogue plugins

        if build_base != builder.parent_images[df_base]:
            # something updated parent_images entry for base without updating
            # the build's base_image; treat it as an error
            raise BaseImageMismatch(
                "Parent image '{}' does not match base_image '{}'"
                .format(builder.parent_images[df_base], build_base)
            )

        unresolved = [key for key, val in builder.parent_images.items() if not val]
        if unresolved:
            # this would generally mean pull_base_image didn't run and/or
            # custom plugins modified parent_images; treat it as an error.
            raise ParentImageUnresolved("Parent image(s) unresolved: {}".format(unresolved))

        missing = [df_img for df_img in dfp.parent_images if df_img not in builder.parent_images]
        if missing:
            # this would indicate another plugin modified parent_images out of sync
            # with the Dockerfile or some other code bug
            raise ParentImageMissing("Lost parent image(s) from Dockerfile: {}".format(missing))

        # docker inspect all parent images so we can address them by Id
        parent_image_ids = {}
        for img, new_img in builder.parent_images.items():
            inspection = builder.tasker.inspect_image(new_img)
            try:
                parent_image_ids[img] = inspection['Id']
            except KeyError:  # unexpected code bugs or maybe docker weirdness
                self.log.error(
                    "Id for image %s is missing in inspection: '%s'",
                    new_img, inspection)
                raise NoIdInspection("Could not inspect Id for image " + new_img)

        # update the parents in Dockerfile
        new_parents = []
        for parent in dfp.parent_images:
            pid = parent_image_ids[parent]
            self.log.info("changed FROM: '%s' -> '%s'", parent, pid)
            new_parents.append(pid)
        dfp.parent_images = new_parents

        # update builder's representation of what will be built
        builder.parent_images = parent_image_ids
        builder.set_base_image(parent_image_ids[df_base])
        self.log.debug(
            "for base image '%s' using local image '%s', id '%s'",
            df_base, build_base, builder.base_image
        )
