"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Classes which implement tasks which builder has to be capable of doing.
Logic above these classes has to set the workflow itself.
"""
import logging
import docker.errors
import atomic_reactor.util
from atomic_reactor.core import ContainerTasker, LastLogger
from atomic_reactor.util import (print_version_of_tools, DockerfileImages)
from osbs.utils import ImageName

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
                 annotations=None, labels=None, skip_layer_squash=False,
                 source_docker_archive=None):
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
        :param source_docker_archive: str, path to docker image archive
        """
        assert fail_reason is None or bool(fail_reason), \
            "If fail_reason provided, can't be falsy"
        # must provide one, not both
        assert not (fail_reason and image_id), \
            "Either fail_reason or image_id should be provided, not both"
        assert not (fail_reason and source_docker_archive), \
            "Either fail_reason or source_docker_archive should be provided, not both"
        assert not (image_id and source_docker_archive), \
            "Either image_id or source_docker_archive should be provided, not both"
        self._logs = logs or []
        self._fail_reason = fail_reason
        self._image_id = image_id
        self._annotations = annotations
        self._labels = labels
        self._skip_layer_squash = skip_layer_squash
        self._source_docker_archive = source_docker_archive

    @classmethod
    def make_remote_image_result(cls, annotations=None, labels=None):
        """Instantiate BuildResult for image not built locally."""
        return cls(
            image_id=cls.REMOTE_IMAGE, annotations=annotations, labels=labels
        )

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

    @property
    def source_docker_archive(self):
        return self._source_docker_archive

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

        self.tasker = ContainerTasker()

        # arguments for build
        self.source = source
        # configuration of source_registy and pull_registries with insecure and
        # dockercfg_path, by registry key
        self.pull_registries = {}
        # moved already to workflow, keeping it here now only because methods in
        # this class are using it, insidebuilder will be removed all together anyway
        self.dockerfile_images = DockerfileImages([])
        self._base_image_inspect = None
        self.parents_pulled = False
        self._parent_images_inspect = {}  # locally available image => inspect
        self.parent_images_digests = {}
        self.image_id = None
        self.built_image_info = None
        self.image = ImageName.parse(image)

    # inspect base image lazily just before it's needed - pre plugins may change the base image
    @property
    def base_image_inspect(self):
        """
        inspect base image

        :return: dict
        """
        if self._base_image_inspect is None:
            base_image = self.dockerfile_images.base_image

            if self.dockerfile_images.base_from_scratch:
                self._base_image_inspect = {}
            elif self.parents_pulled or self.dockerfile_images.custom_base_image:
                try:
                    self._base_image_inspect = \
                        self.tasker.inspect_image(base_image)

                except docker.errors.NotFound as exc:
                    # If the base image cannot be found throw KeyError -
                    # as this property should behave like a dict
                    raise KeyError("Unprocessed base image Dockerfile cannot be inspected") from exc
            else:
                insecure = self.pull_registries[base_image.registry]['insecure']
                dockercfg_path = self.pull_registries[base_image.registry]['dockercfg_path']
                self._base_image_inspect =\
                    atomic_reactor.util.get_inspect_for_image(base_image, base_image.registry,
                                                              insecure, dockercfg_path)

            base_image_str = str(base_image)
            if base_image_str not in self._parent_images_inspect:
                self._parent_images_inspect[base_image_str] = self._base_image_inspect

        return self._base_image_inspect

    def parent_image_inspect(self, image):
        """
        inspect parent image

        :return: dict
        """
        image_name = ImageName.parse(image)
        if image_name not in self._parent_images_inspect:
            if self.parents_pulled:
                self._parent_images_inspect[image_name] = self.tasker.inspect_image(image)
            else:
                insecure = self.pull_registries[image_name.registry]['insecure']
                dockercfg_path = self.pull_registries[image_name.registry]['dockercfg_path']
                self._parent_images_inspect[image_name] =\
                    atomic_reactor.util.get_inspect_for_image(image_name,
                                                              image_name.registry,
                                                              insecure,
                                                              dockercfg_path)

        return self._parent_images_inspect[image_name]

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
