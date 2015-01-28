"""
Classes which implement tasks which builder has to be capable of doing.
Logic above these classes has to set the workflow itself.
"""

import logging

from dock.core import DockerTasker, LastLogger
from dock.util import get_baseimage_from_dockerfile, split_repo_img_name_tag, LazyGit, wait_for_command, \
    join_img_name_tag, figure_out_dockerfile


logger = logging.getLogger(__name__)


class ImageAlreadyBuilt(Exception):
    """ This method expects image not to be built but it already is """


class ImageNotBuilt(Exception):
    """ This method expects image to be already built but it is not """


class BuilderStateMachine(object):
    def __init__(self):
        self.is_built = False

    def _ensure_is_built(self):
        """
        ensure that image is already built

        :return: None
        """
        if not self.is_built:
            logger.error("Image is not built yet!")
            raise ImageNotBuilt()

    def _ensure_not_built(self):
        """
        verify that image wasn't built with 'build' method yet

        :return: None
        """
        if self.is_built:
            logger.error("Image is already built!")
            raise ImageAlreadyBuilt()


class InsideBuilder(LastLogger, LazyGit, BuilderStateMachine):
    """
    This is expected to run within container
    """

    def __init__(self, git_url, image,
                 git_dockerfile_path=None,
                 git_commit=None,
                 tmpdir=None,
                 **kwargs):
        """
        """
        LastLogger.__init__(self)
        LazyGit.__init__(self, git_url, git_commit, tmpdir=tmpdir)
        BuilderStateMachine.__init__(self)

        self.tasker = DockerTasker()

        # arguments for build
        self.git_url = git_url
        self.base_image_id = None
        self.image_id = None
        self.built_image_info = None
        self.base_image_info = None
        self.image = image
        self.reg_uri, self.image_name, self.tag = split_repo_img_name_tag(image)
        self.git_dockerfile_path = git_dockerfile_path
        self.git_commit = git_commit

        # get info about base image from dockerfile
        self.df_path, self.df_dir = figure_out_dockerfile(self.git_path, self.git_dockerfile_path)
        self.df_base_image = get_baseimage_from_dockerfile(self.df_path)
        logger.debug("image specified in dockerfile = '%s'", self.df_base_image)
        self.df_registry, self.base_image_name, self.base_tag = split_repo_img_name_tag(self.df_base_image)
        if not self.base_tag:
            self.base_tag = 'latest'

    def pull_base_image(self, source_registry):
        """
        pull base image

        :param source_registry: str, registry to pull from
        :return:
        """
        logger.info("pull base image from registry")
        self._ensure_not_built()

        if self.df_registry:
            # registry in dockerfile doesn't match provided source registry
            if self.df_registry != source_registry:
                logger.error("registry in dockerfile doesn't match provided source registry, "
                             "dockerfile = '%s', provided = '%s'",
                             self.df_registry, source_registry)
                raise RuntimeError(
                    "Registry specified in dockerfile doesn't match provided one. Dockerfile: '%s', Provided: '%s'"
                    % (self.df_registry, source_registry))

        # this may seem odd; we could pull using registry_img_name, but since registry may be empty
        # let's don't branch here and rather construct reg_uri/img_name again
        base_image = self.tasker.pull_image(self.base_image_name, source_registry, tag=self.base_tag)

        if not self.df_registry:
            response = self.tasker.tag_image(base_image, self.base_image_name, tag=self.base_tag, force=True)
        else:
            response = base_image

        logger.debug("image'%s' is available", response)
        return response

    def build(self):
        """
        build image inside current environment;
        it's expected this may run within (privileged) docker container

        :return: image string (e.g. fedora-python:34)
        """
        logger.info("build image inside current environment")
        self._ensure_not_built()
        logs_gen = self.tasker.build_image_from_path(
            self.df_dir,
            self.image,
        )
        logger.debug("build is submitted, waiting for it to finish")
        self.last_logs = wait_for_command(logs_gen)  # wait for build to finish
        self.is_built = True
        self.built_image_info = self.get_built_image_info()
        # self.base_image_id = self.built_image_info['ParentId']  # parent id is not base image!
        self.image_id = self.built_image_info['Id']
        return self.image

    def push_built_image(self, registry):
        """
        push built image to provided registry

        :param registry: str
        :return: str, image
        """
        logger.info("push built image to registry")
        self._ensure_is_built()
        if not registry:
            logger.warning("no registry specified; skipping")
            return
        return self.tasker.tag_and_push_image(self.image, self.image, registry, tag=self.tag)

    def inspect_base_image(self):
        """
        inspect base image

        :return: dict
        """
        logger.info("inspect base image")
        inspect_data = self.tasker.inspect_image(join_img_name_tag(self.base_image_name, self.base_tag))
        return inspect_data

    def inspect_built_image(self):
        """
        inspect built image

        :return: dict
        """
        logger.info("inspect built image")
        self._ensure_is_built()
        inspect_data = self.tasker.inspect_image(self.image_id)  # dict with lots of data, see man docker-inspect
        return inspect_data

    def get_base_image_info(self):
        """
        query docker about base image

        :return dict
        """
        if self.base_image_info is not None:
            return self.base_image_info
        logger.info("get information about base image")
        image_info = self.tasker.get_image_info_by_image_name(self.base_image_name, tag=self.base_tag)
        items_count = len(image_info)
        if items_count == 1:
            return image_info[0]
        elif items_count <= 0:
            logger.error("image '%s' not found", self.base_image_name)
            raise RuntimeError("image '%s' not found", self.base_image_name)
        else:
            logger.error("multiple (%d) images found for image '%s'", items_count, self.base_image_name)
            raise RuntimeError("multiple (%d) images found for image '%s'" % (items_count, self.base_image_name))

    def get_built_image_info(self):
        """
        query docker about built image

        :return dict
        """
        logger.info("get information about built image")
        self._ensure_is_built()
        image_info = self.tasker.get_image_info_by_image_name(self.image_name)
        items_count = len(image_info)
        if items_count == 1:
            return image_info[0]
        elif items_count <= 0:
            logger.error("image '%s' not found", self.image_name)
            raise RuntimeError("image '%s' not found" % self.image_name)
        else:
            logger.error("multiple (%d) images found for image '%s'", items_count, self.image_name)
            raise RuntimeError("multiple (%d) images found for image '%s'" % (items_count, self.image_name))
