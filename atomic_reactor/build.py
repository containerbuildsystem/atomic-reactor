"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Classes which implement tasks which builder has to be capable of doing.
Logic above these classes has to set the workflow itself.
"""
import json

import logging
import docker.errors
import atomic_reactor.util
from atomic_reactor.core import DockerTasker, LastLogger
from atomic_reactor.util import (ImageName, print_version_of_tools, df_parser)
from atomic_reactor.constants import DOCKERFILE_FILENAME

logger = logging.getLogger(__name__)


class ImageAlreadyBuilt(Exception):
    """ This method expects image not to be built but it already is """


class ImageNotBuilt(Exception):
    """ This method expects image to be already built but it is not """


class BuilderStateMachine(object):
    def __init__(self):
        self.is_built = False
        self.image = None

    def ensure_is_built(self):
        """
        ensure that image is already built

        :return: None
        """
        if not self.is_built:
            logger.error("image '%s' is not built yet!", self.image)
            raise ImageNotBuilt()

    def ensure_not_built(self):
        """
        verify that image wasn't built with 'build' method yet

        :return: None
        """
        if self.is_built:
            logger.error("image '%s' is already built!", self.image)
            raise ImageAlreadyBuilt()


class BuildResult(object):

    REMOTE_IMAGE = object()

    def __init__(self, logs=None, fail_reason=None, image_id=None,
                 annotations=None, labels=None, skip_layer_squash=False):
        """
        :param logs: iterable of log lines (without newlines)
        :param fail_reason: str, description of failure or None if successful
        :param image_id: str, ID of built container image
        :param annotations: dict, data captured during build step which
                            should be annotated to OpenShift build
        :param labels: dict, data captured during build step which
                       should be set as labels on OpenShift build
        :param skip_layer_squash: boolean, direct post-build plugins not
                                  to squash image layers for this build
        """
        assert fail_reason is None or bool(fail_reason), \
            "If fail_reason provided, can't be falsy"
        # must provide one, not both
        assert bool(fail_reason) != bool(image_id), \
            "Either fail_reason or image_id should be provided, not both"
        self._logs = logs or []
        self._fail_reason = fail_reason
        self._image_id = image_id
        self._annotations = annotations
        self._labels = labels
        self._skip_layer_squash = skip_layer_squash

    @staticmethod
    def make_remote_image_result(annotations=None, labels=None):
        """Instantiate BuildResult for image not built locally."""
        return BuildResult(image_id=BuildResult.REMOTE_IMAGE,
                           annotations=annotations, labels=labels)

    @property
    def logs(self):
        return self._logs

    @property
    def fail_reason(self):
        return self._fail_reason

    def is_failed(self):
        return self._fail_reason is not None

    @property
    def image_id(self):
        return self._image_id

    @property
    def annotations(self):
        return self._annotations

    @property
    def labels(self):
        return self._labels

    @property
    def skip_layer_squash(self):
        return self._skip_layer_squash

    def is_image_available(self):
        return self._image_id and self._image_id is not self.REMOTE_IMAGE


class InsideBuilder(LastLogger, BuilderStateMachine):
    """
    This is expected to run within container
    """

    def __init__(self, source, image, **kwargs):
        """
        """
        LastLogger.__init__(self)
        BuilderStateMachine.__init__(self)

        print_version_of_tools()

        self.tasker = DockerTasker()

        info, version = self.tasker.get_info(), self.tasker.get_version()
        logger.debug(json.dumps(info, indent=2))
        logger.info(json.dumps(version, indent=2))

        # arguments for build
        self.source = source
        self.base_image = None
        self.original_base_image = None
        self._base_image_inspect = None
        self._parents_pulled = False
        self.parent_images = {}  # dockerfile image => locally available image
        self._parent_images_inspect = {}  # locally available image => inspect
        self.image_id = None
        self.built_image_info = None
        self.image = ImageName.parse(image)

        # get info about base image from dockerfile
        build_file_path, build_file_dir = self.source.get_build_file_path()

        self.df_dir = build_file_dir
        self._df_path = None

        # If the Dockerfile will be entirely generated from the container.yaml
        # (in the Flatpak case, say), then a plugin needs to create the Dockerfile
        # and set the base image
        if build_file_path.endswith(DOCKERFILE_FILENAME):
            self.set_df_path(build_file_path)

    @property
    def df_path(self):
        if self._df_path is None:
            raise AttributeError("Dockerfile has not yet been generated")

        return self._df_path

    def set_df_path(self, path):
        self._df_path = path
        dfp = df_parser(path)
        base = dfp.baseimage
        if base is None:
            raise RuntimeError("no base image specified in Dockerfile")
        self.set_base_image(base)
        logger.debug("base image specified in dockerfile = '%s'", self.base_image)
        self.parent_images.clear()
        for image in dfp.parent_images:
            self.parent_images[image] = None

    def set_base_image(self, base_image, parents_pulled=True, insecure=False):
        logger.info("setting base image to '%s'", base_image)
        self.base_image = ImageName.parse(base_image)
        self.original_base_image = self.original_base_image or self.base_image
        self.parent_images[str(self.original_base_image)] = base_image
        self._parents_pulled = parents_pulled
        self._base_image_insecure = insecure

    # inspect base image lazily just before it's needed - pre plugins may change the base image
    @property
    def base_image_inspect(self):
        """
        inspect base image

        :return: dict
        """
        if self._base_image_inspect is None:
            if self._parents_pulled:
                try:
                    self._base_image_inspect = self.tasker.inspect_image(self.base_image)

                except docker.errors.NotFound:
                    # If the base image cannot be found throw KeyError -
                    # as this property should behave like a dict
                    raise KeyError("Unprocessed base image Dockerfile cannot be inspected")
            else:
                self._base_image_inspect =\
                    atomic_reactor.util.get_inspect_for_image(self.base_image,
                                                              self.base_image.registry,
                                                              self._base_image_insecure)

            base_image_str = str(self.base_image)
            if base_image_str not in self._parent_images_inspect:
                self._parent_images_inspect[base_image_str] = self._base_image_inspect

        return self._base_image_inspect

    def parent_image_inspect(self, image):
        """
        inspect parent image

        :return: dict
        """
        if image not in self._parent_images_inspect:
            if self._parents_pulled:
                self._parent_images_inspect[image] = self.tasker.inspect_image(image)
            else:
                image_obj = ImageName.parse(image)
                self._parent_images_inspect[image] =\
                    atomic_reactor.util.get_inspect_for_image(image_obj,
                                                              image_obj.registry,
                                                              self._base_image_insecure)

        return self._parent_images_inspect[image]

    def inspect_built_image(self):
        """
        inspect built image

        :return: dict
        """
        logger.info("inspecting built image '%s'", self.image_id)
        self.ensure_is_built()
        # dict with lots of data, see man docker-inspect
        inspect_data = self.tasker.inspect_image(self.image_id)
        return inspect_data

    def get_base_image_info(self):
        """
        query docker about base image

        :return dict
        """
        logger.info("getting information about base image '%s'", self.base_image)
        image_info = self.tasker.get_image_info_by_image_name(self.base_image)
        items_count = len(image_info)
        if items_count == 1:
            return image_info[0]
        elif items_count <= 0:
            logger.error("image '%s' not found", self.base_image)
            raise RuntimeError("image '%s' not found", self.base_image)
        else:
            logger.error("multiple (%d) images found for image '%s'", items_count,
                         self.base_image)
            raise RuntimeError("multiple (%d) images found for image '%s'" % (items_count,
                                                                              self.base_image))

    def get_built_image_info(self):
        """
        query docker about built image

        :return dict
        """
        logger.info("getting information about built image '%s'", self.image)
        image_info = self.tasker.get_image_info_by_image_name(self.image)
        items_count = len(image_info)
        if items_count == 1:
            return image_info[0]
        elif items_count <= 0:
            logger.error("image '%s' not found", self.image)
            raise RuntimeError("image '%s' not found" % self.image)
        else:
            logger.error("multiple (%d) images found for image '%s'", items_count, self.image)
            raise RuntimeError("multiple (%d) images found for image '%s'" % (items_count,
                                                                              self.image))
