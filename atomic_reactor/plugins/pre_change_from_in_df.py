"""
Copyright (c) 2015-2018 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pre-build plugin that changes the parent images used in FROM instructions
to the more specific names given by the builder.
"""
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import df_parser, base_image_is_scratch


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
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow):
        """
        constructor

        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        """
        # call parent constructor
        super(ChangeFromPlugin, self).__init__(tasker, workflow)

    def _sanity_check(self, df_base, builder_base, builder):
        if builder_base != builder.dockerfile_images[df_base]:
            # something updated parent_images entry for base without updating
            # the build's base_image; treat it as an error
            raise BaseImageMismatch(
                "Parent image '{}' for df_base {} does not match base_image '{}'"
                .format(builder.dockerfile_images[df_base], df_base, builder_base)
            )

    def run(self):
        builder = self.workflow.builder
        dfp = df_parser(builder.df_path)
        builder.original_df = dfp.content

        df_base = dfp.baseimage
        build_base = builder.dockerfile_images.base_image

        if not self.workflow.builder.dockerfile_images.base_from_scratch:
            # do some sanity checks to defend against bugs and rogue plugins
            self._sanity_check(dfp.baseimage, build_base, builder)

        self.log.info("parent_images '%s'", builder.dockerfile_images.keys())
        unresolved = [key for key, val in builder.dockerfile_images.items() if not val]
        if unresolved:
            # this would generally mean pull_base_image didn't run and/or
            # custom plugins modified parent_images; treat it as an error.
            raise ParentImageUnresolved("Parent image(s) unresolved: {}".format(unresolved))

        # check for lost parent images
        missing_set = set()
        for df_img in dfp.parent_images:
            if base_image_is_scratch(df_img):
                continue
            try:
                builder.dockerfile_images[df_img]
            except KeyError:
                missing_set.add(df_img)
        if missing_set:
            # this would indicate another plugin modified parent_images out of sync
            # with the Dockerfile or some other code bug
            raise ParentImageMissing("Lost parent image(s) from Dockerfile: {}".format(missing_set))

        # docker inspect all parent images so we can address them by Id
        parent_image_ids = {}
        new_parents = []

        for df_img in dfp.parent_images:
            if base_image_is_scratch(df_img):
                new_parents.append(df_img)
                continue
            local_image = builder.dockerfile_images[df_img]
            inspection = builder.parent_image_inspect(local_image)

            try:
                parent_image_ids[df_img] = inspection['Id']
                new_parents.append(inspection['Id'])
            except KeyError as exc:  # unexpected code bugs or maybe docker weirdness
                self.log.error(
                    "Id for image %s is missing in inspection: '%s'",
                    df_img, inspection)
                raise NoIdInspection("Could not inspect Id for image " + df_img) from exc

        # update builder's representation of what will be built
        for df_img in dfp.parent_images:
            if base_image_is_scratch(df_img):
                continue
            builder.dockerfile_images[df_img] = parent_image_ids[df_img]

        # update parent_images in Dockerfile
        dfp.parent_images = new_parents

        if builder.dockerfile_images.base_from_scratch:
            return

        self.log.debug(
            "for base image '%s' using local image '%s', id '%s'",
            df_base, build_base, parent_image_ids[df_base]
        )
