"""
Naming Conventions
==================

registry.somewhere/image_name:tag
|-----------------|               registry, reg_uri
|----------------------------|    repository
                  |----------|    image name
                             |--| tag
                  |-------------| image
|-------------------------------| image

I've tried to be as much consistent (man pages were source) with docker as possible


"""
import os
import shutil
import logging
import tempfile
import datetime

import git
import docker
from docker.errors import APIError

from dock.constants import CONTAINER_SHARE_PATH, BUILD_JSON
from dock.util import join_repo_img_name_tag, \
    join_repo_img_name, join_img_name_tag, wait_for_command, clone_git_repo, figure_out_dockerfile


DOCKER_SOCKET_PATH = '/var/run/docker.sock'

logger = logging.getLogger(__name__)


class LastLogger(object):
    """
    provide method for getting last log
    """

    def __init__(self, *args, **kwargs):
        self._last_logs = []

    @property
    def last_logs(self):
        return self._last_logs

    @last_logs.setter
    def last_logs(self, value):
        self._last_logs = value


class BuildContainerFactory(object):
    """
    set of methods for building images inside containers
    """

    def __init__(self):
        self.tasker = DockerTasker()

    def _check_build_input(self, image, args_path):
        """
        Internal method, validate provided args.

        :param image: str
        :param args_path: str, path dir which is mounter inside container
        :return: None
        :raises RuntimeError
        """
        try:
            with open(os.path.join(args_path, BUILD_JSON)) as json_args:
                logger.debug("build image = '%s', args = '%s'", image, json_args.read())
        except (IOError, OSError) as ex:
            logger.error("Unable to open json arguments: '%s'", repr(ex))
            raise RuntimeError("Unable to open json arguments: '%s'" % repr(ex))

        if not self.tasker.image_exists(image):
            logger.error("Provided build image doesn't exist: '%s'", image)
            raise RuntimeError("Provided build image doesn't exist: '%s'" % image)

    def build_image_dockerhost(self, build_image, json_args_path):
        """
        Build docker image within a build image using docker from host (mount docker socket inside container).
        There are possible races here. Use wisely.

        This operation is asynchronous and you should wait for container to finish.

        :param build_image: str, name of image where build is performed
        :param json_args_path: str, this dir is mounted inside build container and used
                               as a way to transport data between host and buildroot; there
                               has to be a file inside this dir with name dock.BUILD_JSON which
                               is used to feed build
        :return: str, container id
        """
        logger.info("build image in container using docker from host")

        self._check_build_input(build_image, json_args_path)

        if not os.path.exists(DOCKER_SOCKET_PATH):
            logger.error("Looks like docker is not running because there is no socket at: %s", DOCKER_SOCKET_PATH)
            raise RuntimeError("docker socket not found: %s" % DOCKER_SOCKET_PATH)

        volume_bindings = {
            DOCKER_SOCKET_PATH: {
                'bind': DOCKER_SOCKET_PATH,
                'ro': True,
            },
            json_args_path: {
                'bind': CONTAINER_SHARE_PATH,
                'rw': True,
            },
        }

        container_id = self.tasker.run(
            build_image,
            create_kwargs={'volumes': [DOCKER_SOCKET_PATH, json_args_path]},
            start_kwargs={'binds': volume_bindings},
        )

        return container_id

    def build_image_privileged_container(self, build_image, json_args_path):
        """
        build image inside privileged container: this will run another docker instance inside

        This operation is asynchronous and you should wait for container to finish.

        :param build_image: str, name of image where build is performed
        :param json_args_path: str, this dir is mounted inside build container and used
                               as a way to transport data between host and buildroot; there
                               has to be a file inside this dir with name dock.BUILD_JSON which
                               is used to feed build
        :return: dict, keys container_id and stream
        """
        logger.info("build image inside privileged container")

        self._check_build_input(build_image, json_args_path)

        container_id = self.tasker.run(
            build_image,
            create_kwargs={'volumes': [json_args_path]},
            start_kwargs={'binds': {json_args_path: {'bind': CONTAINER_SHARE_PATH, 'rw': True}},
                          'privileged': True}
        )

        return container_id


class DockerTasker(LastLogger):
    def __init__(self, *args, **kwargs):
        super(DockerTasker, self).__init__(*args, **kwargs)
        self.d = docker.Client(base_url='unix:/%s' % DOCKER_SOCKET_PATH)

    def build_image_from_path(self, path, image, stream=False, use_cache=False, remove_im=True):
        """
        build image from provided path and tag it

        this operation is asynchronous and you should consume returned generator in order to wait
        for build to finish

        :param path: str
        :param image: str, repository[:tag]
        :param stream: bool, True returns generator, False returns str
        :param use_cache: bool, True if you want to use cache
        :param remove_im: bool, remove intermediate containers produced during docker build
        :return: generator
        """
        logger.info("build image from provided path")
        logger.debug("image = '%s', path = '%s'", image, path)
        prior_to_build = datetime.datetime.now()
        response = self.d.build(path=path, tag=image, stream=stream, nocache=not use_cache,
                                rm=remove_im)  # returns generator
        logger.info("build finished")
        logger.debug("build time = %s", datetime.datetime.now() - prior_to_build)
        return response

    def build_image_from_git(self, url, image, git_path=None, git_commit=None, copy_dockerfile_to=None,
                             stream=False, use_cache=False):
        """
        build image from provided url and tag it

        this operation is asynchronous and you should consume returned generator in order to wait
        for build to finish

        :param url: str
        :param image: str, repository[:tag]
        :param git_path: str, path to dockerfile within gitrepo
        :param copy_dockerfile_to: str, copy dockerfile to provided path
        :param stream: bool, True returns generator, False returns str
        :param use_cache: bool, True if you want to use cache
        :return: generator
        """
        logger.info("build image from provided git repo specified as URL")
        logger.debug("url = '%s', image = '%s', git_path = '%s', copy_df_to='%s'",
                     url, image, git_path, copy_dockerfile_to)
        temp_dir = tempfile.mkdtemp()
        response = None
        try:
            clone_git_repo(url, temp_dir, git_commit)
            df_path, df_dir = figure_out_dockerfile(temp_dir, git_path)
            if copy_dockerfile_to:  # TODO: pre build plugin
                shutil.copyfile(os.path.join(df_dir, "Dockerfile"), copy_dockerfile_to)
            response = self.build_image_from_path(df_dir, image, stream=stream, use_cache=use_cache)
        finally:
            try:
                shutil.rmtree(temp_dir)
            except (IOError, OSError) as ex:
                # no idea why this is happening
                logger.warning("Failed to remove dir '%s': '%s'", temp_dir, repr(ex))
        logger.info("build finished")
        return response

    def run(self, image, command=None, create_kwargs=None, start_kwargs=None):
        """
        create container from provided image and start it

        for more info, see documentation of REST API calls:
         * containers/{}/start
         * container/create

        :param image: str
        :param command: str
        :param create_kwargs: dict, kwargs for docker.create_container
        :param start_kwargs: dict, kwargs for docker.start
        :return: str, container id
        """
        logger.info("create container from image and run it")
        create_kwargs = create_kwargs or {}
        start_kwargs = start_kwargs or {}
        logger.debug("image = '%s', command = '%s', create_kwargs = '%s', start_kwargs = '%s'",
                     image, command, create_kwargs, start_kwargs)
        container_dict = self.d.create_container(image, command=command, **create_kwargs)
        container_id = container_dict['Id']
        logger.debug("container_id = '%s'", container_id)
        self.d.start(container_id, **start_kwargs)  # returns None
        return container_id

    def commit_container(self, container_id, repository=None, message=None):
        """
        create image from provided container

        :param container_id: str
        :param repository: str (repo/image_name)
        :param message: str
        :return: image_id
        """
        logger.info("commit container")
        logger.debug("container_id = '%s', repository = '%s', message = '%s'",
                     container_id, repository, message)
        response = self.d.commit(container_id, repository=repository, message=message)
        logger.debug("response = '%s'", response)
        try:
            return response['Id']
        except KeyError:
            logger.error("ID missing from commit response")
            raise RuntimeError("ID missing from commit response")

    def get_image_info_by_image_id(self, image_id):
        """
        using `docker images`, provide information about an image

        :param image_id: str, hash of image to get info
        :return: str or None
        """
        logger.info("get info about provided image specified by image_id")
        logger.debug("image_id = '%s'", image_id)
        # returns list of
        # {u'Created': 1414577076,
        #  u'Id': u'3ab9a7ed8a169ab89b09fb3e12a14a390d3c662703b65b4541c0c7bde0ee97eb',
        #  u'ParentId': u'a79ad4dac406fcf85b9c7315fe08de5b620c1f7a12f45c8185c843f4b4a49c4e',
        #  u'RepoTags': [u'buildroot-fedora:latest'],
        #  u'Size': 0,
        #  u'VirtualSize': 856564160}
        images = self.d.images()
        try:
            image_dict = [i for i in images if i['Id'] == image_id][0]
        except IndexError:
            logger.info("image not found")
            return None
        else:
            return image_dict

    def get_image_info_by_image_name(self, image_name, reg_uri='', tag=None):
        """
        using `docker images`, provide information about an image

        :param image_name: str, name of image (without tag!)
        :param reg_uri: str, optional registry
        :return: list of dicts
        """
        logger.info("get info about provided image specified by name")
        logger.debug("image_name = '%s', registry = '%s', tag = '%s'", image_name, reg_uri, tag)
        # returns list of
        # {u'Created': 1414577076,
        #  u'Id': u'3ab9a7ed8a169ab89b09fb3e12a14a390d3c662703b65b4541c0c7bde0ee97eb',
        #  u'ParentId': u'a79ad4dac406fcf85b9c7315fe08de5b620c1f7a12f45c8185c843f4b4a49c4e',
        #  u'RepoTags': [u'buildroot-fedora:latest'],
        #  u'Size': 0,
        #  u'VirtualSize': 856564160}
        repository = join_repo_img_name(reg_uri, image_name)
        images = self.d.images(name=repository)
        if tag:
            # tag is specified, we are looking for the exact image
            image = join_repo_img_name_tag(reg_uri, image_name, tag)
            for found_image in images:
                if image in found_image['RepoTags']:
                    logger.debug("image '%s' found", image)
                    return [found_image]
            images = []  # image not found

        logger.debug("%d matching images found", len(images))
        return images

    def pull_image(self, image_name, reg_uri, tag=''):
        """
        pull provided image from registry

        :param image_name: str, image name
        :param reg_uri: str, reg.com
        :param tag: str, v1
        :return: str, image (reg.om/img:v1)
        """
        logger.info("pull image from registry")
        logger.debug("image = '%s', registry = '%s', tag = '%s'", image_name, reg_uri, tag)
        image = join_repo_img_name_tag(reg_uri, image_name, tag)  # e.g. registry.com/image_name:1
        try:
            logs_gen = self.d.pull(image, insecure_registry=True, stream=True)
        except TypeError:
            # because changing api is fun
            logs_gen = self.d.pull(image, stream=True)
        self.last_logs = wait_for_command(logs_gen)
        return image

    def tag_image(self, image, target_image_name, reg_uri='', tag='', force=False):
        """
        tag provided image with specified image_name, registry and tag

        :param image: str (reg.com/img:v1)
        :param target_image_name: str, img
        :param reg_uri: str, reg.com
        :param tag: str, v1
        :param force: bool, force tag the image?
        :return: str, image (reg.om/img:v1)
        """
        logger.info("tag image")
        logger.debug("image = '%s', target_image_name = '%s', reg_uri = '%s', tag = '%s'",
                     image, target_image_name, reg_uri, tag)
        repository = join_repo_img_name(reg_uri, target_image_name)
        response = self.d.tag(image, repository, tag=tag, force=force)  # returns True/False
        if not response:
            logger.error("failed to tag image")
            raise RuntimeError("Failed to tag image '%s': repository = '%s', tag = '%s'" %
                               image, repository, tag)
        return join_img_name_tag(repository, tag)  # this will be the proper name, not just repo/img

    def push_image(self, image):
        """
        push provided image to registry

        :param image: str
        :return: str, logs from push
        """
        logger.info("push image")
        logger.debug("image: '%s'", image)
        try:
            # push returns string composed of newline separated jsons; exactly what 'docker push' outputs
            logs = self.d.push(image, insecure_registry=True, stream=False)
        except TypeError:
            # because changing api is fun
            logs = self.d.push(image, stream=False)
        return logs

    def tag_and_push_image(self, image, target_image_name, reg_uri='', tag=''):
        """
        tag provided image and push it to registry

        :param image: str (reg.com/img:v1)
        :param target_image_name: str, img
        :param reg_uri: str, reg.com
        :param tag: str, v1
        :return: str, image (reg.om/img:v1)
        """
        logger.info("tag and push image")
        logger.debug("image = '%s', target_image_name = '%s', reg_uri = '%s', tag = '%s'",
                     image, target_image_name, reg_uri, tag)
        final_tag = self.tag_image(image, target_image_name, reg_uri=reg_uri, tag=tag)
        return self.push_image(final_tag)

    def inspect_image(self, image_id):
        """
        return detailed metadata about provided image (see 'man docker-inspect')

        :param image_id: str
        :return: dict
        """
        logger.info("inspect image")
        logger.debug("image_id = '%s'", image_id)
        image_metadata = self.d.inspect_image(image_id)
        return image_metadata

    def remove_image(self, image_id, force=False, noprune=False):
        """
        remove provided image from filesystem

        :param image_id: str
        :param noprune: bool, keep untagged parents?
        :param force: bool, force remove -- just trash it no matter what
        :return: None
        """
        logger.info("remove image from filesystem")
        logger.debug("image_id = '%s'", image_id)
        self.d.remove_image(image_id, force=force, noprune=noprune)  # returns None

    def remove_container(self, container_id):
        """
        remove provided container from filesystem

        :param container_id: str
        :return: None
        """
        logger.info("remove container from filesystem")
        logger.debug("container_id = '%s'", container_id)
        self.d.remove_container(container_id)  # returns None

    def logs(self, container_id, stderr=True, stream=True):
        """
        acquire output (stdout, stderr) from provided container

        :param container_id: str
        :param stderr: True, False
        :param stream: if True, return as generator
        :return: either generator, or list of strings
        """
        logger.info("get stdout of container")
        logger.debug("container_id = '%s', stream = '%s'", container_id, stream)
        response = self.d.logs(container_id, stdout=True, stderr=stderr, stream=stream)
        if not stream:
            response = response.decode("utf-8")  # py2 & 3 compat
            response = [line for line in response.split('\n') if line]
        return response

    def wait(self, container_id):
        """
        wait for container to finish the job (may run infinitely)

        :param container_id: str
        :return: int, exit code
        """
        logger.info("wait for container to finish")
        logger.debug("container = '%s'", container_id)
        response = self.d.wait(container_id)  # returns exit code as int
        logger.debug("container finished with exit code %s", response)
        return response

    def image_exists(self, image_id):
        """
        does provided image exists?

        :param image_id: str
        :return: True if exists, False if not
        """
        logger.info("does image exists?")
        logger.debug("image_id = '%s'", image_id)
        try:
            response = self.d.inspect_image(image_id) is not None
        except APIError:
            response = False
        logger.debug("image exists: %s", response)
        return response
