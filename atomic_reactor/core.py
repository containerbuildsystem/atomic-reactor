"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Naming Conventions
==================

registry.somewhere/namespace/image_name:tag
|-----------------|                          registry, reg_uri
                  |---------|                namespace
|--------------------------------------|     repository
                  |--------------------|     image name
                                        |--| tag
                  |------------------------| image
|------------------------------------------| image

I've tried to be as much consistent (man pages were source) with docker as possible


"""
import os
import shutil
import logging
import tempfile
import json

import docker
from docker.errors import APIError

from atomic_reactor.constants import CONTAINER_SHARE_PATH, CONTAINER_SHARE_SOURCE_SUBDIR,\
        BUILD_JSON, DOCKER_SOCKET_PATH
from atomic_reactor.source import get_source_instance_for
from atomic_reactor.util import ImageName, wait_for_command, clone_git_repo, figure_out_dockerfile


logger = logging.getLogger(__name__)


class LastLogger(object):
    """
    provide method for getting last log
    """

    def __init__(self):
        self._last_logs = []

    @property
    def last_logs(self):
        """ logs from last operation """
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
                logger.debug("build input: image = '%s', args = '%s'", image, json_args.read())
        except (IOError, OSError) as ex:
            logger.error("unable to open json arguments: %r", ex)
            raise RuntimeError("Unable to open json arguments: %r" % ex)

        if not self.tasker.image_exists(image):
            logger.error("provided build image doesn't exist: '%s'", image)
            raise RuntimeError("Provided build image doesn't exist: '%s'" % image)

    def _obtain_source_from_path_if_needed(self, local_path, container_path=CONTAINER_SHARE_PATH):
        # TODO: maybe we should do this for any provider? (if we expand to various providers
        #  like mercurial, we don't to force container to have mercurial installed, etc.)
        build_json_path = os.path.join(local_path, BUILD_JSON)
        with open(build_json_path, 'r') as fp:
            build_json = json.load(fp)
        source = get_source_instance_for(build_json['source'], tmpdir=local_path)
        if source.provider == 'path':
            logger.debug('copying source from %s to %s', source.schemeless_path, local_path)
            source.get()
            logger.debug('verifying that %s exists: %s', local_path, os.path.exists(local_path))
            # now modify the build json
            build_json['source']['uri'] =\
                    'file://' + os.path.join(container_path, CONTAINER_SHARE_SOURCE_SUBDIR)
            with open(build_json_path, 'w') as fp:
                json.dump(build_json, fp)
        # else we do nothing

    @staticmethod
    def _volume_bind_understands_mode():
        # docker.utils.convert_volume_binds() understands 'mode' since docker-py-1.3.0
        # returns ['/a/:/b/:rw'] with docker-py < 1.3.0 and ['/a/:/b/:ro,Z'] with >= 1.3.0
        bind = docker.utils.convert_volume_binds({'/a/': {'bind': '/b/', 'mode': 'ro,Z'}})
        if bind and len(bind[0].split(':')) > 2 and bind[0].split(':')[2] == 'ro,Z':
            return True
        return False

    def build_image_dockerhost(self, build_image, json_args_path):
        """
        Build docker image inside privileged container using docker from host
        (mount docker socket inside container).
        There are possible races here. Use wisely.

        This operation is asynchronous and you should wait for container to finish.

        :param build_image: str, name of image where build is performed
        :param json_args_path: str, this dir is mounted inside build container and used
                               as a way to transport data between host and buildroot; there
                               has to be a file inside this dir with name atomic_reactor.BUILD_JSON which
                               is used to feed build
        :return: str, container id
        """
        logger.info("building image '%s' in container using docker from host", build_image)

        self._check_build_input(build_image, json_args_path)
        self._obtain_source_from_path_if_needed(json_args_path, CONTAINER_SHARE_PATH)

        if not os.path.exists(DOCKER_SOCKET_PATH):
            logger.error("looks like docker is not running because there is no socket at: %s", DOCKER_SOCKET_PATH)
            raise RuntimeError("docker socket not found: %s" % DOCKER_SOCKET_PATH)

        volume_bindings = {
            DOCKER_SOCKET_PATH: {
                'bind': DOCKER_SOCKET_PATH,
            },
            json_args_path: {
                'bind': CONTAINER_SHARE_PATH,
            },
        }

        if self._volume_bind_understands_mode():
            volume_bindings[DOCKER_SOCKET_PATH]['mode'] = 'ro'
            volume_bindings[json_args_path]['mode'] = 'rw,Z'
        else:
            volume_bindings[DOCKER_SOCKET_PATH]['ro'] = True
            volume_bindings[json_args_path]['rw'] = True

        logger.debug('build json mounted in container: %s',
                open(os.path.join(json_args_path, BUILD_JSON)).read())
        container_id = self.tasker.run(
            ImageName.parse(build_image),
            create_kwargs={'volumes': [DOCKER_SOCKET_PATH, json_args_path]},
            start_kwargs={'binds': volume_bindings,
                          'privileged': True}
        )

        return container_id

    def build_image_privileged_container(self, build_image, json_args_path):
        """
        Build image inside privileged container: this will run another docker instance inside

        This operation is asynchronous and you should wait for container to finish.

        :param build_image: str, name of image where build is performed
        :param json_args_path: str, this dir is mounted inside build container and used
                               as a way to transport data between host and buildroot; there
                               has to be a file inside this dir with name atomic_reactor.BUILD_JSON which
                               is used to feed build
        :return: dict, keys container_id and stream
        """
        logger.info("building image '%s' inside privileged container", build_image)

        self._check_build_input(build_image, json_args_path)
        self._obtain_source_from_path_if_needed(json_args_path, CONTAINER_SHARE_PATH)

        volume_bindings = {
            json_args_path: {
                'bind': CONTAINER_SHARE_PATH,
            },
        }

        if self._volume_bind_understands_mode():
            volume_bindings[json_args_path]['mode'] = 'rw,Z'
        else:
            volume_bindings[json_args_path]['rw'] = True

        logger.debug('build json mounted in container: %s',
                open(os.path.join(json_args_path, BUILD_JSON)).read())
        container_id = self.tasker.run(
            ImageName.parse(build_image),
            create_kwargs={'volumes': [json_args_path]},
            start_kwargs={'binds': volume_bindings,
                          'privileged': True}
        )

        return container_id


class DockerTasker(LastLogger):
    def __init__(self, base_url=None, timeout=120, **kwargs):
        """
        Constructor

        :param base_url: str, docker connection URL
        :param timeout: int, timeout for docker client
        """
        super(DockerTasker, self).__init__(**kwargs)

        client_kwargs = {}
        if base_url:
            client_kwargs['base_url'] = base_url
        elif os.environ.get('DOCKER_CONNECTION'):
            client_kwargs['base_url'] = os.environ['DOCKER_CONNECTION']

        if hasattr(docker, 'AutoVersionClient'):
            client_kwargs['version'] = 'auto'

        self.d = docker.Client(timeout=timeout, **client_kwargs)

    def build_image_from_path(self, path, image, stream=False, use_cache=False, remove_im=True):
        """
        build image from provided path and tag it

        this operation is asynchronous and you should consume returned generator in order to wait
        for build to finish

        :param path: str
        :param image: ImageName, name of the resulting image
        :param stream: bool, True returns generator, False returns str
        :param use_cache: bool, True if you want to use cache
        :param remove_im: bool, remove intermediate containers produced during docker build
        :return: generator
        """
        logger.info("building image '%s' from path '%s'", image, path)
        try:
            response = self.d.build(path=path, tag=image.to_str(), stream=stream, nocache=not use_cache,
                                    rm=remove_im, pull=False)  # returns generator
        except TypeError:
            # because changing api is fun
            response = self.d.build(path=path, tag=image.to_str(), stream=stream, nocache=not use_cache,
                                    rm=remove_im)  # returns generator
        return response

    def build_image_from_git(self, url, image, git_path=None, git_commit=None, copy_dockerfile_to=None,
                             stream=False, use_cache=False):
        """
        build image from provided url and tag it

        this operation is asynchronous and you should consume returned generator in order to wait
        for build to finish

        :param url: str
        :param image: ImageName, name of the resulting image
        :param git_path: str, path to dockerfile within gitrepo
        :param copy_dockerfile_to: str, copy dockerfile to provided path
        :param stream: bool, True returns generator, False returns str
        :param use_cache: bool, True if you want to use cache
        :return: generator
        """
        logger.info("building image '%s' from git repo '%s' specified as URL '%s'",
                image, git_path, url)
        logger.info("will copy Dockerfile to '%s'", copy_dockerfile_to)
        temp_dir = tempfile.mkdtemp()
        response = None
        try:
            clone_git_repo(url, temp_dir, git_commit)
            df_path, df_dir = figure_out_dockerfile(temp_dir, git_path)
            if copy_dockerfile_to:  # TODO: pre build plugin
                shutil.copyfile(df_path, copy_dockerfile_to)
            response = self.build_image_from_path(df_dir, image, stream=stream, use_cache=use_cache)
        finally:
            try:
                shutil.rmtree(temp_dir)
            except (IOError, OSError) as ex:
                # no idea why this is happening
                logger.warning("Failed to remove dir '%s': %r", temp_dir, ex)
        logger.info("build finished")
        return response

    def run(self, image, command=None, create_kwargs=None, start_kwargs=None):
        """
        create container from provided image and start it

        for more info, see documentation of REST API calls:
         * containers/{}/start
         * container/create

        :param image: ImageName or string, name or id of the image
        :param command: str
        :param create_kwargs: dict, kwargs for docker.create_container
        :param start_kwargs: dict, kwargs for docker.start
        :return: str, container id
        """
        logger.info("creating container from image '%s' and running it", image)
        create_kwargs = create_kwargs or {}
        start_kwargs = start_kwargs or {}
        logger.debug("image = '%s', command = '%s', create_kwargs = '%s', start_kwargs = '%s'",
                     image, command, create_kwargs, start_kwargs)
        if isinstance(image, ImageName):
            image = image.to_str()
        container_dict = self.d.create_container(image, command=command, **create_kwargs)
        container_id = container_dict['Id']
        logger.debug("container_id = '%s'", container_id)
        self.d.start(container_id, **start_kwargs)  # returns None
        return container_id

    def commit_container(self, container_id, image=None, message=None):
        """
        create image from provided container

        :param container_id: str
        :param image: ImageName
        :param message: str
        :return: image_id
        """
        logger.info("committing container '%s'", container_id)
        logger.debug("container_id = '%s', image = '%s', message = '%s'",
                     container_id, image, message)
        tag = None
        if image:
            tag = image.tag
            image = image.to_str(tag=False)
        response = self.d.commit(container_id, repository=image, tag=tag, message=message)
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
        logger.info("getting info about provided image specified by image_id '%s'", image_id)
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

    def get_image_info_by_image_name(self, image, exact_tag=True):
        """
        using `docker images`, provide information about an image

        :param image: ImageName, name of image
        :param exact_tag: bool, if false then return info for all images of the
                          given name regardless what their tag is
        :return: list of dicts
        """
        logger.info("getting info about provided image specified by name '%s'", image)
        logger.debug("image_name = '%s'", image)

        # returns list of
        # {u'Created': 1414577076,
        #  u'Id': u'3ab9a7ed8a169ab89b09fb3e12a14a390d3c662703b65b4541c0c7bde0ee97eb',
        #  u'ParentId': u'a79ad4dac406fcf85b9c7315fe08de5b620c1f7a12f45c8185c843f4b4a49c4e',
        #  u'RepoTags': [u'buildroot-fedora:latest'],
        #  u'Size': 0,
        #  u'VirtualSize': 856564160}
        images = self.d.images(name=image.to_str(tag=False))
        if exact_tag:
            # tag is specified, we are looking for the exact image
            for found_image in images:
                if image.to_str(explicit_tag=True) in found_image['RepoTags']:
                    logger.debug("image '%s' found", image)
                    return [found_image]
            images = []  # image not found

        logger.debug("%d matching images found", len(images))
        return images

    def pull_image(self, image, insecure=False):
        """
        pull provided image from registry

        :param image_name: ImageName, image to pull
        :param insecure: bool, allow connecting to registry over plain http
        :return: str, image (reg.om/img:v1)
        """
        logger.info("pulling image '%s' from registry", image)
        logger.debug("image = '%s', insecure = '%s'", image, insecure)
        try:
            logs_gen = self.d.pull(image.to_str(tag=False), tag=image.tag, insecure_registry=insecure, stream=True)
        except TypeError:
            # because changing api is fun
            logs_gen = self.d.pull(image.to_str(tag=False), tag=image.tag, stream=True)
        command_result = wait_for_command(logs_gen)
        self.last_logs = command_result.logs
        return image.to_str()

    def tag_image(self, image, target_image, force=False):
        """
        tag provided image with specified image_name, registry and tag

        :param image: str or ImageName, image to tag
        :param target_image: ImageName, new name for the image
        :param force: bool, force tag the image?
        :return: str, image (reg.om/img:v1)
        """
        logger.info("tagging image '%s' as '%s'", image, target_image)
        logger.debug("image = '%s', target_image_name = '%s'", image, target_image)
        if not isinstance(image, ImageName):
            image = ImageName.parse(image)

        if image != target_image:
            response = self.d.tag(
                image.to_str(),
                target_image.to_str(tag=False),
                tag=target_image.tag,
                force=force)  # returns True/False
            if not response:
                logger.error("failed to tag image")
                raise RuntimeError("Failed to tag image '%s': target_image = '%s'" %
                        image.to_str(), target_image)
        else:
            logger.debug('image already tagged correctly, nothing to do')
        return target_image.to_str()  # this will be the proper name, not just repo/img

    def push_image(self, image, insecure=False):
        """
        push provided image to registry

        :param image: ImageName
        :param insecure: bool, allow connecting to registry over plain http
        :return: str, logs from push
        """
        logger.info("pushing image '%s'", image)
        logger.debug("image: '%s', insecure: '%s'", image, insecure)
        try:
            # push returns string composed of newline separated jsons; exactly what 'docker push' outputs
            logs = self.d.push(image.to_str(tag=False), tag=image.tag, insecure_registry=insecure, stream=True)
        except TypeError:
            # because changing api is fun
            logs = self.d.push(image.to_str(tag=False), tag=image.tag, stream=True)

        command_result = wait_for_command(logs)
        self.last_logs = command_result.logs
        if command_result.is_failed():
            raise RuntimeError("Failed to push image %s" % image)
        return command_result.parsed_logs

    def tag_and_push_image(self, image, target_image, insecure=False, force=False):
        """
        tag provided image and push it to registry

        :param image: str or ImageName, image id or name
        :param target_image: ImageName, img
        :param insecure: bool, allow connecting to registry over plain http
        :param force: bool, force the tag?
        :return: str, image (reg.com/img:v1)
        """
        logger.info("tagging and pushing image '%s' as '%s'", image, target_image)
        logger.debug("image = '%s', target_image = '%s'", image, target_image)
        self.tag_image(image, target_image, force=force)
        return self.push_image(target_image, insecure=insecure)

    def inspect_image(self, image_id):
        """
        return detailed metadata about provided image (see 'man docker-inspect')

        :param image_id: str or ImageName, id or name of the image
        :return: dict
        """
        logger.info("inspecting image '%s'", image_id)
        logger.debug("image_id = '%s'", image_id)
        if isinstance(image_id, ImageName):
            image_id = image_id.to_str()
        image_metadata = self.d.inspect_image(image_id)
        return image_metadata

    def remove_image(self, image_id, force=False, noprune=False):
        """
        remove provided image from filesystem

        :param image_id: str or ImageName
        :param noprune: bool, keep untagged parents?
        :param force: bool, force remove -- just trash it no matter what
        :return: None
        """
        logger.info("removing image '%s' from filesystem", image_id)
        logger.debug("image_id = '%s'", image_id)
        if isinstance(image_id, ImageName):
            image_id = image_id.to_str()
        self.d.remove_image(image_id, force=force, noprune=noprune)  # returns None

    def remove_container(self, container_id, force=False):
        """
        remove provided container from filesystem

        :param container_id: str
        :param force: bool, remove forcefully?
        :return: None
        """
        logger.info("removing container '%s' from filesystem", container_id)
        logger.debug("container_id = '%s'", container_id)
        self.d.remove_container(container_id, force=force)  # returns None

    def logs(self, container_id, stderr=True, stream=True):
        """
        acquire output (stdout, stderr) from provided container

        :param container_id: str
        :param stderr: True, False
        :param stream: if True, return as generator
        :return: either generator, or list of strings
        """
        logger.info("getting stdout of container '%s'", container_id)
        logger.debug("container_id = '%s', stream = '%s'", container_id, stream)
        # returns bytes
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
        logger.info("waiting for container '%s' to finish", container_id)
        logger.debug("container = '%s'", container_id)
        response = self.d.wait(container_id)  # returns exit code as int
        logger.debug("container finished with exit code %s", response)
        return response

    def image_exists(self, image_id):
        """
        does provided image exists?

        :param image_id: str or ImageName
        :return: True if exists, False if not
        """
        logger.info("checking whether image '%s' exists", image_id)
        logger.debug("image_id = '%s'", image_id)
        try:
            response = self.d.inspect_image(image_id)
        except APIError as ex:
            logger.warning(repr(ex))
            response = False
        else:
            response = response is not None
        logger.debug("image exists: %s", response)
        return response

    def get_info(self):
        """
        get info about used docker environment

        :return: dict, json output of `docker info`
        """
        return self.d.info()

    def get_version(self):
        """
        get version of used docker environment

        :return: dict, json output of `docker version`
        """
        return self.d.version()
