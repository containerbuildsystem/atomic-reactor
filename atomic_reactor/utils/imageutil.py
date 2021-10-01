"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
# OSBS2 TBD


def get_inspect_for_image(image):
    # util.get_inspect_for_image(image, registry, insecure=False, dockercfg_path=None)
    # or use skopeo
    # insecure = self.pull_registries[base_image.registry]['insecure']
    # dockercfg_path = self.pull_registries[base_image.registry]['dockercfg_path']
    # self._base_image_inspect =\
    #     atomic_reactor.util.get_inspect_for_image(base_image, base_image.registry, insecure,
    # dockercfg_path)
    return {}


def get_image_history(image):
    # get image history with skopeo / registry api
    return []


def inspect_built_image():
    # get output image final/arch specific from somewhere
    # and call get_inspect_for_image
    return {}


def base_image_inspect():
    # get base image from workflow.dockerfile_images
    # and call get_inspect_for_image
    return {}


def remove_image(image, force=False):
    # self.tasker.remove_image(image, force=force)
    # most likely won't be needed at all
    return {}


def tag_image(image, new_image):
    # self.tasker.tag_image(image, new_image)
    return True


def get_image(image):
    # self.tasker.get_image(image)
    # use skopeo copy
    return {}
