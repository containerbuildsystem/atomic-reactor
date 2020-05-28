"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Classes which implement tasks which builder has to be capable of doing.
Logic above these classes has to set the workflow itself.
"""
from __future__ import absolute_import

import re
from textwrap import dedent

import logging
import docker.errors
import atomic_reactor.util
from atomic_reactor.core import ContainerTasker, LastLogger
from atomic_reactor.util import (print_version_of_tools, df_parser,
                                 base_image_is_scratch, base_image_is_custom)
from atomic_reactor.constants import DOCKERFILE_FILENAME
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
                 oci_image_path=None):
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
        :param oci_image_path: str, path to OCI image directory
        """
        assert fail_reason is None or bool(fail_reason), \
            "If fail_reason provided, can't be falsy"
        # must provide one, not both
        assert not (fail_reason and image_id), \
            "Either fail_reason or image_id should be provided, not both"
        assert not (fail_reason and oci_image_path), \
            "Either fail_reason or oci_image_path should be provided, not both"
        assert not (image_id and oci_image_path), \
            "Either image_id or oci_image_path should be provided, not both"
        self._logs = logs or []
        self._fail_reason = fail_reason
        self._image_id = image_id
        self._annotations = annotations
        self._labels = labels
        self._skip_layer_squash = skip_layer_squash
        self._oci_image_path = oci_image_path

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
    def oci_image_path(self):
        return self._oci_image_path

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
        self.base_image = None
        # configuration of source_registy and pull_registries with insecure and
        # dockercfg_path, by registry key
        self.pull_registries = {}
        self.original_base_image = None
        self._base_image_inspect = None
        self.parents_pulled = False
        self.parent_images = {}  # dockerfile ImageName => locally available ImageName
        self._parent_images_inspect = {}  # locally available image => inspect
        self.parents_ordered = []
        self.parent_images_digests = {}
        self.image_id = None
        self.built_image_info = None
        self.image = ImageName.parse(image)
        self.base_from_scratch = False
        # last parent in Dockerfile is custom base image,
        # used for plugins custom base image handling
        self.custom_base_image = False
        # any parent in Dockerfile is custom base image,
        # used for plugins custom base image handling
        self.custom_parent_image = False

        # get info about base image from dockerfile
        build_file_path, build_file_dir = self.source.get_build_file_path()

        self.df_dir = build_file_dir
        self._df_path = None
        self.original_df = None
        self.buildargs = {}  # --buildargs for container build

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
        custom_base_images = set()
        for image in dfp.parent_images:
            image_name = ImageName.parse(image)
            if base_image_is_scratch(image_name.get_repo()):
                image_name.tag = None
                self.parents_ordered.append(image_name.to_str())
                continue
            image_str = image_name.to_str()
            if base_image_is_custom(image_str):
                custom_base_images.add(image_str)
                self.custom_parent_image = True
            self.parents_ordered.append(image_str)

            # we are setting values None, because we know parent but we don't
            # have local copy yet
            # if image is base image we want to keep image instance in key,
            # so it is same as original_base_image instance
            if self.original_base_image.to_str() == image_str:
                self.parent_images[self.original_base_image] = None
                continue

            self.parent_images[image_name] = None

        if len(custom_base_images) > 1:
            raise NotImplementedError("multiple different custom base images"
                                      " aren't allowed in Dockerfile")

        # validate user has not specified COPY --from=image
        builders = []
        for stmt in dfp.structure:
            if stmt['instruction'] == 'FROM':
                # extract "bar" from "foo as bar" and record as build stage
                match = re.search(r'\S+ \s+  as  \s+ (\S+)', stmt['value'], re.I | re.X)
                builders.append(match.group(1) if match else None)
            elif stmt['instruction'] == 'COPY':
                match = re.search(r'--from=(\S+)', stmt['value'], re.I)
                if not match:
                    continue
                stage = match.group(1)
                # error unless the --from is the index or name of a stage we've seen
                if any(stage in [str(idx), builder] for idx, builder in enumerate(builders)):
                    continue
                raise RuntimeError(dedent("""\
                    OSBS does not support COPY --from unless it matches a build stage.
                    Dockerfile instruction was:
                      {}
                    To use an image with COPY --from, specify it in a stage with FROM, e.g.
                      FROM {} AS source
                      FROM ...
                      COPY --from=source <src> <dest>
                    """).format(stmt['content'], stage))

    def recreate_parent_images(self):
        # recreate parent_images to update hashes
        # when ImageName key is added to parent_images
        # the hash for key is calculated
        # but later when we are enclosing that ImageName key
        # it won't automatically rehash
        parent_images = {}
        for key, val in self.parent_images.items():
            parent_images[key] = val
        self.parent_images = parent_images

    def set_base_image(self, base_image, parents_pulled=True):
        self.base_from_scratch = base_image_is_scratch(base_image)
        if not self.custom_base_image:
            self.custom_base_image = base_image_is_custom(base_image)
        self.base_image = ImageName.parse(base_image)
        self.original_base_image = self.original_base_image or self.base_image
        self.recreate_parent_images()

        if not self.base_from_scratch:
            self.parent_images[self.original_base_image] = self.base_image
        self.parents_pulled = parents_pulled
        logger.info("set base image to '%s' with original base '%s'", self.base_image,
                    self.original_base_image)

    # inspect base image lazily just before it's needed - pre plugins may change the base image
    @property
    def base_image_inspect(self):
        """
        inspect base image

        :return: dict
        """
        if self._base_image_inspect is None:

            if self.base_from_scratch:
                self._base_image_inspect = {}
            elif self.parents_pulled or self.custom_base_image:
                try:
                    self._base_image_inspect = self.tasker.inspect_image(self.base_image)

                except docker.errors.NotFound:
                    # If the base image cannot be found throw KeyError -
                    # as this property should behave like a dict
                    raise KeyError("Unprocessed base image Dockerfile cannot be inspected")
            else:
                insecure = self.pull_registries[self.base_image.registry]['insecure']
                dockercfg_path = self.pull_registries[self.base_image.registry]['dockercfg_path']
                self._base_image_inspect =\
                    atomic_reactor.util.get_inspect_for_image(self.base_image,
                                                              self.base_image.registry,
                                                              insecure,
                                                              dockercfg_path)

            base_image_str = str(self.base_image)
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

    def get_base_image_info(self):
        """
        query docker about base image

        :return dict
        """
        if self.base_from_scratch:
            return
        logger.info("getting information about base image '%s'", self.base_image)
        image_info = self.tasker.get_image_info_by_image_name(self.base_image)
        items_count = len(image_info)
        if items_count == 1:
            return image_info[0]
        elif items_count <= 0:
            logger.error("image '%s' not found", self.base_image)
            raise RuntimeError("image '%s' not found" % self.base_image)
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

    def parent_images_to_str(self):
        results = {}
        for base_image_name, parent_image_name in self.parent_images.items():
            base_str = str(base_image_name)
            parent_str = str(parent_image_name)
            if base_image_name and parent_image_name:
                results[base_str] = parent_str
            else:
                logger.debug("None in: base %s has parent %s", base_str, parent_str)

        return results
