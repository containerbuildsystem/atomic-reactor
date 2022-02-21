"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import functools
import subprocess
import logging
import tarfile
import json

from typing import Optional, Union, Dict, List, Any
from pathlib import Path

from osbs.utils import ImageName

from atomic_reactor import config
from atomic_reactor import util
from atomic_reactor.types import ImageInspectionData
from atomic_reactor.utils import retries

logger = logging.getLogger(__name__)


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
        """Inspect an image.

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

    def extract_file_from_image(self, image: Union[str, ImageName],
                                src_path: str, dst_path: str) -> None:
        """
        Extract file or directory from image at src_path to dst_path
        using 'oc image extract' command. This command has some peculiar
        behaviour that the users of this method should be aware of.
            - the dst_path must be an existing empty dir, otherwise
              the extraction fails
            - file permissions of the extracted files are not preserved
            - trying to extract nonexistent file fails silently
            - when extracting whole dir, it matters if src_path ends
              in / (e.g. /usr/local/bin vs /usr/local/bin/).
              If slash is used, only the files in the directory will
              be extraced. Else, the directory together will the files
              will be extracted


        :param image: Union[str, ImageName], image pullspec from which to extract
        :param src_path: str, path inside the image that points to file or directory
                             that will be extracted
        :param dst_path: str, path where to export file/dir
        """
        if any(Path(dst_path).iterdir()):
            raise ValueError(f'the destination directory {dst_path} must be empty')

        cmd = ['oc', 'image', 'extract', f'{image}', '--path', f'{src_path}:{dst_path}']

        try:
            retries.run_cmd(cmd)
        except subprocess.CalledProcessError as e:
            logger.error("Image file extraction failed\n%s", e.output)
            raise

        # check if something was extracted, as the extraction can fail
        # silently when extracting nonexisting files
        if not any(Path(dst_path).iterdir()):
            raise ValueError(f'Extraction failed, files at path {src_path} not found in the image')

    def download_image_archive_tarball(self, image: Union[str, ImageName], path: str) -> None:
        """Downloads image archive tarball to path.

        :param image: Union[str, ImageName], image pullspec to download
        :param path: str, path including the filename of the tarball
        """
        cmd = ['skopeo', 'copy', f'docker://{image}', f'docker-archive:{path}']
        try:
            retries.run_cmd(cmd)
        except subprocess.CalledProcessError as e:
            logger.error("Image archive download failed:\n%s", e.output)
            raise

    def get_uncompressed_image_layer_sizes(self, path: str) -> List[Dict[str, Any]]:
        """Returns data about the uncompressed image layer sizes

        :param path: str path to a image archive tarball
        :return: List[Dict[str, Any]], List of dicts, where each dict
                 contains layer digest and the size of the layer in bytes
        """
        with tarfile.open(path) as tar:
            manifest_file = tar.extractfile('manifest.json')
            if not manifest_file:
                raise ValueError(f'manifest.json from {path} is not a regular file')
            manifest = json.load(manifest_file)
            # manifest.json can contain additional entries for parent images
            # but we expect only one
            if len(manifest) > 1:
                raise ValueError('manifest.json file has multiple entries, expected only one')
            layers = manifest[0]['Layers']
            config_filename = manifest[0]['Config']
            config_file = tar.extractfile(config_filename)
            if not config_file:
                raise ValueError(f'config file {config_filename} from {path} is not a regular file')
            config = json.load(config_file)
            diff_ids = config['rootfs']['diff_ids']
            return [
                {"diff_id": diff_id, "size": tar.getmember(layer).size}
                for (diff_id, layer) in zip(diff_ids, layers)
            ]

    def extract_filesystem_layer(self, src_path: str, dst_path: str) -> str:
        """Extract filesystem layer from image archive tarball and
        saves it at dst_path. This is meant for flatpaks and will work
        only when the archive has 1 layer.

        :param src_path: str, path to image archive tarball
        :param dst_path: str, path where the layer will be copied
        :return: str, relative path (from dst_path) to filesystem layer
        """
        with tarfile.open(src_path) as tar:
            manifest_file = tar.extractfile('manifest.json')
            if not manifest_file:
                raise ValueError(f'manifest.json from {src_path} is not a regular file')
            manifest = json.load(manifest_file)
            # manifest.json can contain additional entries for parent images
            # but we expect only one
            if len(manifest) > 1:
                raise ValueError('manifest.json file has multiple entries, expected only one')
            layers = manifest[0]['Layers']
            if len(layers) > 1:
                raise ValueError(f'Tarball at {src_path} has more than 1 layer')

            layer_file = tar.getmember(layers[0])
            tar.extract(layer_file, dst_path)

        return layers[0]
