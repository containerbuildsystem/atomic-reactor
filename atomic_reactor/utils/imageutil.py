"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import functools
from typing import Optional, Union

from osbs.utils import ImageName

from atomic_reactor import config
from atomic_reactor import util
from atomic_reactor.types import ImageInspectionData


def image_is_inspectable(image: Union[str, ImageName]) -> bool:
    """Check if we should expect the image to be inspectable."""
    im = str(image)
    return not (util.base_image_is_scratch(im) or util.base_image_is_custom(im))


class ImageUtil:
    """Convenience class for working with images relevant to the build process.

    Supports e.g. inspecting the base image and other parent images.
    """

    def __init__(self, dockerfile_images: util.DockerfileImages, conf: config.Configuration):
        """Init an ImageUtil.

        :param dockerfile_images: information about the image references in the Dockerfile
        :param conf: atomic-reactor configuration
        """
        self._dockerfile_images = dockerfile_images
        self._conf = conf

    def set_dockerfile_images(self, dockerfile_images: util.DockerfileImages) -> None:
        """Set a new dockerfile_images instance."""
        self._dockerfile_images = dockerfile_images

    def get_inspect_for_image(
        self, image: Union[str, ImageName], platform: Optional[str] = None
    ) -> ImageInspectionData:
        """Inspect an image. Should mainly be used to inspect parent images.

        The image must be inspectable, passing a non-inspectable image is an error.

        The result is cached, this method will not query the registry more than once
        (if successful) for the same image reference (within one Python process).

        :param image: The image to inspect
        :param platform: Optionally, inspect the base image for a specific platform.
            This can be either the platform name (e.g. x86_64) or the GOARCH name (amd64).
        """
        if not image_is_inspectable(image):
            raise ValueError(f"{image!r} is not inspectable")

        goarch = self._conf.platform_to_goarch_mapping[platform]
        return self._cached_inspect_image(str(image), goarch)

    def base_image_inspect(self, platform: Optional[str] = None) -> ImageInspectionData:
        """Inspect the base image (the parent image for the final build stage).

        If the base image is scratch or custom, return an empty dict.

        :param platform: Optionally, inspect the base image for a specific platform.
            This can be either the platform name (e.g. x86_64) or the GOARCH name (amd64).
        """
        base_image: Union[str, ImageName] = self._dockerfile_images.base_image
        if not image_is_inspectable(base_image):
            return {}

        return self.get_inspect_for_image(base_image, platform)

    @functools.lru_cache(maxsize=None)
    def _cached_inspect_image(
        self, image: str, goarch: Optional[str] = None
    ) -> ImageInspectionData:
        # Important: this method must take the image name as a string, not an ImageName.
        #   The functools cache decorator maps inputs to outputs in a dict. While the
        #   ImageName object *is* hashable, it is also mutable, which can lead to very
        #   unpleasant bugs.
        parsed_image = ImageName.parse(image)
        client = self._get_registry_client(parsed_image.registry)
        return client.get_inspect_for_image(parsed_image, goarch)

    @functools.lru_cache(maxsize=None)
    def _get_registry_client(self, registry: str) -> util.RegistryClient:
        session = util.RegistrySession.create_from_config(self._conf, registry)
        return util.RegistryClient(session)


# OSBS2 TBD


def get_image_history(image):
    # get image history with skopeo / registry api
    return []


def inspect_built_image():
    # get output image final/arch specific from somewhere
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
