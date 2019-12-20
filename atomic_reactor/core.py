"""
Copyright (c) 2015, 2019 Red Hat, Inc
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
from __future__ import absolute_import

import os
import shutil
import logging
import tempfile
import json
import requests
import time
import docker
import atomic_reactor.util
from docker.errors import APIError
from functools import wraps

from atomic_reactor.constants import (CONTAINER_SHARE_PATH, CONTAINER_SHARE_SOURCE_SUBDIR,
                                      BUILD_JSON, DOCKER_SOCKET_PATH, DOCKER_MAX_RETRIES,
                                      DOCKER_BACKOFF_FACTOR, DOCKER_CLIENT_STATUS_RETRY,
                                      CONTAINER_IMAGEBUILDER_BUILD_METHOD,
                                      CONTAINER_DOCKERPY_BUILD_METHOD)

from atomic_reactor.source import get_source_instance_for
from atomic_reactor.util import ImageName, figure_out_build_file, Dockercfg
from osbs.utils import clone_git_repo

from urllib3.exceptions import (InsecureRequestWarning, ProtocolError,
                                ReadTimeoutError)
from urllib3 import disable_warnings
disable_warnings(InsecureRequestWarning)

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
            logger.error("unable to open json arguments: %s", ex)
            raise RuntimeError("Unable to open json arguments: %s" % ex)

        if not self.tasker.image_exists(image):
            logger.error("provided build image doesn't exist: '%s'", image)
            raise RuntimeError("Provided build image doesn't exist: '%s'" % image)

    def _obtain_source_from_path_if_needed(self, local_path, container_path=CONTAINER_SHARE_PATH):
        # TODO: maybe we should do this for any provider? If we expand to various providers
        # like mercurial, then we don't want to force the container to have mercurial
        # installed, etc.
        build_json_path = os.path.join(local_path, BUILD_JSON)
        with open(build_json_path, 'r') as fp:
            build_json = json.load(fp)
        source = get_source_instance_for(build_json['source'], tmpdir=local_path)
        if source.provider == 'path':
            logger.debug('copying source from %s to %s', source.schemeless_path, local_path)
            source.get()
            logger.debug('verifying that %s exists: %s', local_path, os.path.exists(local_path))
            # now modify the build json
            build_json['source']['uri'] = 'file://' + os.path.join(container_path,
                                                                   CONTAINER_SHARE_SOURCE_SUBDIR)
            with open(build_json_path, 'w') as fp:
                json.dump(build_json, fp)
        # else we do nothing

    def build_image_dockerhost(self, build_image, json_args_path):
        """
        Build docker image inside privileged container using docker from host
        (mount docker socket inside container).
        There are possible races here. Use wisely.

        This operation is asynchronous and you should wait for container to finish.

        :param build_image: str, name of image where build is performed
        :param json_args_path: str, this dir is mounted inside build container and used
                               as a way to transport data between host and buildroot; there
                               has to be a file inside this dir with name
                               atomic_reactor.BUILD_JSON which is used to feed build
        :return: str, container id
        """
        logger.info("building image '%s' in container using docker from host", build_image)

        self._check_build_input(build_image, json_args_path)
        self._obtain_source_from_path_if_needed(json_args_path, CONTAINER_SHARE_PATH)

        if not os.path.exists(DOCKER_SOCKET_PATH):
            logger.error("looks like docker is not running because there is no socket at: %s",
                         DOCKER_SOCKET_PATH)
            raise RuntimeError("docker socket not found: %s" % DOCKER_SOCKET_PATH)

        volume_bindings = {
            DOCKER_SOCKET_PATH: {
                'bind': DOCKER_SOCKET_PATH,
                'mode': 'ro',
            },
            json_args_path: {
                'bind': CONTAINER_SHARE_PATH,
                'mode': 'rw,Z',
            },
        }

        with open(os.path.join(json_args_path, BUILD_JSON)) as fp:
            logger.debug('build json mounted in container: %s', fp.read())

        container_id = self.tasker.run(
            ImageName.parse(build_image),
            create_kwargs={'volumes': [DOCKER_SOCKET_PATH, json_args_path]},
            volume_bindings=volume_bindings,
            privileged=True,
        )

        return container_id

    def build_image_privileged_container(self, build_image, json_args_path):
        """
        Build image inside privileged container: this will run another docker instance inside

        This operation is asynchronous and you should wait for container to finish.

        :param build_image: str, name of image where build is performed
        :param json_args_path: str, this dir is mounted inside build container and used
                               as a way to transport data between host and buildroot; there
                               has to be a file inside this dir with name
                               atomic_reactor.BUILD_JSON which is used to feed build
        :return: dict, keys container_id and stream
        """
        logger.info("building image '%s' inside privileged container", build_image)

        self._check_build_input(build_image, json_args_path)
        self._obtain_source_from_path_if_needed(json_args_path, CONTAINER_SHARE_PATH)

        volume_bindings = {
            json_args_path: {
                'bind': CONTAINER_SHARE_PATH,
                'mode': 'rw,Z',
            },
        }

        with open(os.path.join(json_args_path, BUILD_JSON)) as fp:
            logger.debug('build json mounted in container: %s', fp.read())

        container_id = self.tasker.run(
            ImageName.parse(build_image),
            create_kwargs={'volumes': [json_args_path]},
            volume_bindings=volume_bindings,
            privileged=True,
        )

        return container_id


def retry(function, *args, **kwargs):
    retry_times = int(kwargs.pop('retry', 0))
    retry_delay = DOCKER_BACKOFF_FACTOR
    retry_client_statuses = DOCKER_CLIENT_STATUS_RETRY

    for counter in range(retry_times + 1):
        try:
            return function(*args, **kwargs)
        except APIError as e:
            if (e.response.status_code in retry_client_statuses and counter != retry_times):
                logger.info("retrying %s on %s", function, e.response.status_code)
                time.sleep(retry_delay * (2 ** counter))
            else:
                raise


class RetryGeneratorException(Exception):
    """Retry generator pull/push failed"""
    def __init__(self, message, error, *args, **kwargs):
        super(RetryGeneratorException, self).__init__(message, *args, **kwargs)
        self.error = error


class WrappedDocker(object):
    def __init__(self, **kwargs):
        self.retry_times = kwargs.pop('retry', None)

        try:
            # docker-py 2.x
            self.wrapped = docker.APIClient(**kwargs)
        except AttributeError:
            # docker-py 1.x
            self.wrapped = docker.Client(**kwargs)

    def __getattr__(self, attr):
        orig_attr = getattr(self.wrapped, attr)

        if callable(orig_attr):
            @wraps(orig_attr)
            def hooked(*args, **kwargs):
                return retry(orig_attr, *args, retry=self.retry_times, **kwargs)
            return hooked
        else:
            return orig_attr

    def _export(self, container, chunk_size):
        # Older versions of docker-python don't handle streaming responses
        # efficiently. Emulate the fix in:
        # https://github.com/docker/docker-py/commit/581ccc9f7e8e189248054268c98561ca775bd3d7
        # by going straight to the responses API.

        url = self.wrapped._url("/containers/{0}/export", container)
        res = self.wrapped.get(url, stream=True)
        self.wrapped._raise_for_status(res)
        return res.iter_content(chunk_size=chunk_size)

    def export(self, container, chunk_size=1024*2048):
        return retry(self._export, container, chunk_size, retry=self.retry_times)


class ContainerTasker(LastLogger):
    def __init__(self, base_url=None, retry_times=DOCKER_MAX_RETRIES,
                 timeout=120, **kwargs):
        self.build_method = None
        self.base_url = base_url
        self.retry_times = retry_times
        self.timeout = timeout
        self._tasker = None
        self.kwargs = kwargs

        super(ContainerTasker, self).__init__(**kwargs)

    @property
    def tasker(self):
        if self._tasker is None:
            if self.build_method is None:
                raise AttributeError("Container task type not yet decided")

            if self.build_method in (CONTAINER_IMAGEBUILDER_BUILD_METHOD,
                                     CONTAINER_DOCKERPY_BUILD_METHOD):
                self._tasker = DockerTasker(self.base_url, self.retry_times, self.timeout,
                                            **self.kwargs)
                logger.info('ContainerTasker will use DockerTasker')
            else:
                raise AttributeError('build method "%s" is not valid to determine '
                                     'Container tasker type' % self.build_method)

            info, version = self._tasker.get_info(), self._tasker.get_version()
            logger.debug(json.dumps(info, indent=2))
            logger.info(json.dumps(version, indent=2))

        return self._tasker

    def build_image_from_path(self, *args, **kwargs):
        return self.tasker.build_image_from_path(*args, **kwargs)

    def build_image_from_git(self, *args, **kwargs):
        return self.tasker.build_image_from_git(*args, **kwargs)

    def run(self, *args, **kwargs):
        return self.tasker.run(*args, **kwargs)

    def commit_container(self, *args, **kwargs):
        return self.tasker.commit_container(*args, **kwargs)

    def create_container(self, *args, **kwargs):
        return self.tasker.create_container(*args, **kwargs)

    def export_container(self, *args, **kwargs):
        return self.tasker.export_container(*args, **kwargs)

    def get_image_info_by_image_id(self, *args, **kwargs):
        return self.tasker.get_image_info_by_image_id(*args, **kwargs)

    def get_image_info_by_image_name(self, *args, **kwargs):
        return self.tasker.get_image_info_by_image_name(*args, **kwargs)

    def pull_image(self, *args, **kwargs):
        return self.tasker.pull_image(*args, **kwargs)

    def tag_image(self, *args, **kwargs):
        return self.tasker.tag_image(*args, **kwargs)

    def login(self, *args, **kwargs):
        return self.tasker.login(*args, **kwargs)

    def push_image(self, *args, **kwargs):
        return self.tasker.push_image(*args, **kwargs)

    def tag_and_push_image(self, *args, **kwargs):
        return self.tasker.tag_and_push_image(*args, **kwargs)

    def inspect_image(self, *args, **kwargs):
        return self.tasker.inspect_image(*args, **kwargs)

    def remove_image(self, *args, **kwargs):
        return self.tasker.remove_image(*args, **kwargs)

    def remove_container(self, *args, **kwargs):
        return self.tasker.remove_container(*args, **kwargs)

    def logs(self, *args, **kwargs):
        return self.tasker.logs(*args, **kwargs)

    def wait(self, *args, **kwargs):
        return self.tasker.wait(*args, **kwargs)

    def image_exists(self, *args, **kwargs):
        return self.tasker.image_exists(*args, **kwargs)

    def get_info(self, *args, **kwargs):
        return self.tasker.get_info(*args, **kwargs)

    def get_version(self, *args, **kwargs):
        return self.tasker.get_version(*args, **kwargs)

    def get_volumes_for_container(self, *args, **kwargs):
        return self.tasker.get_volumes_for_container(*args, **kwargs)

    def remove_volume(self, *args, **kwargs):
        return self.tasker.remove_volume(*args, **kwargs)

    def get_image_history(self, *args, **kwargs):
        return self.tasker.get_image_history(*args, **kwargs)

    def get_image(self, *args, **kwargs):
        return self.tasker.get_image(*args, **kwargs)

    def import_image_from_stream(self, *args, **kwargs):
        return self.tasker.import_image_from_stream(*args, **kwargs)

    def get_archive(self, *args, **kwargs):
        return self.tasker.get_archive(*args, **kwargs)

    def cleanup_containers(self, *args, **kwargs):
        return self.tasker.cleanup_containers(*args, **kwargs)


class CommonTasker(LastLogger):
    def __init__(self, **kwargs):
        """
        Constructor for common tasker, which has shared methods
        for specific taskers
        """
        super(CommonTasker, self).__init__(**kwargs)
        self.retry_times = None
        self.d = None

    def retry_generator(self, function, *args, **kwargs):
        retry_times = int(kwargs.pop('retry_times', self.retry_times))
        retry_delay = DOCKER_BACKOFF_FACTOR
        retry_client_statuses = DOCKER_CLIENT_STATUS_RETRY

        for counter in range(retry_times + 1):
            exc = None
            context = None
            try:
                logs_gen = function(*args, **kwargs)
                cmd_result = atomic_reactor.util.wait_for_command(logs_gen)
            except ProtocolError as e:
                exc = e
                context = e.args
            except APIError as e:
                exc = e
                context = e.response.content
            except ReadTimeoutError as e:
                exc = e
                context = e.args
            else:
                if cmd_result.is_failed():
                    exc = cmd_result.error_detail
                    context = cmd_result.error

            if exc:
                if counter == retry_times or \
                    (isinstance(exc, APIError) and
                     exc.response.status_code not in retry_client_statuses):
                    raise RetryGeneratorException("Failed to %s image %s: %r" %
                                                  (function.__name__, args, context),
                                                  exc)

                logger.info("retrying %s - %s on %r", function.__name__, args, context)
                time.sleep(retry_delay * (2 ** counter))
                continue

            return cmd_result

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


class DockerTasker(CommonTasker):
    def __init__(self, base_url=None, retry_times=DOCKER_MAX_RETRIES,
                 timeout=120, **kwargs):
        """
        Constructor

        :param base_url: str, docker connection URL
        :param timeout: int, timeout for docker client
        """
        super(DockerTasker, self).__init__(**kwargs)

        client_kwargs = {'timeout': timeout}
        if base_url:
            client_kwargs['base_url'] = base_url
        elif os.environ.get('DOCKER_CONNECTION'):
            client_kwargs['base_url'] = os.environ['DOCKER_CONNECTION']

        if hasattr(docker, 'AutoVersionClient'):
            client_kwargs['version'] = 'auto'
        self.retry_times = retry_times
        client_kwargs['retry'] = self.retry_times

        self.d = WrappedDocker(**client_kwargs)

    def build_image_from_path(self, path, image, use_cache=False, remove_im=True, buildargs=None):
        """
        build image from provided path and tag it

        this operation is asynchronous and you should consume returned generator in order to wait
        for build to finish

        :param path: str
        :param image: ImageName, name of the resulting image
        :param use_cache: bool, True if you want to use cache
        :param remove_im: bool, remove intermediate containers produced during docker build
        :param buildargs: dict, build-time variables --build-arg
        :return: generator
        """
        logger.info("building image '%s' from path '%s'", image, path)
        # returns generator
        response = self.d.build(path=path, tag=image.to_str(), nocache=not use_cache, decode=True,
                                rm=remove_im, forcerm=True, pull=False, buildargs=buildargs)
        return response

    def build_image_from_git(self, url, image, git_path=None, git_commit=None,
                             copy_dockerfile_to=None, use_cache=False, buildargs=None):
        """
        build image from provided url and tag it

        this operation is asynchronous and you should consume returned generator in order to wait
        for build to finish

        :param url: str
        :param image: ImageName, name of the resulting image
        :param git_path: str, path to dockerfile within gitrepo
        :param copy_dockerfile_to: str, copy dockerfile to provided path
        :param use_cache: bool, True if you want to use cache
        :param buildargs: dict, build-time variables --build-arg
        :return: generator
        """
        logger.info("building image '%s' from git repo '%s' specified as URL '%s'",
                    image, git_path, url)
        logger.info("will copy Dockerfile to '%s'", copy_dockerfile_to)
        temp_dir = tempfile.mkdtemp()
        response = None
        try:
            clone_git_repo(url, temp_dir, git_commit)
            build_file_path, build_file_dir = figure_out_build_file(temp_dir, git_path)
            if copy_dockerfile_to:  # TODO: pre build plugin
                shutil.copyfile(build_file_path, copy_dockerfile_to)
            response = self.build_image_from_path(build_file_dir, image, use_cache=use_cache,
                                                  buildargs=buildargs)
        finally:
            try:
                shutil.rmtree(temp_dir)
            except (IOError, OSError) as ex:
                # no idea why this is happening
                logger.warning("Failed to remove dir '%s': %s", temp_dir, ex)
        logger.info("build finished")
        return response

    def run(self, image, command=None, create_kwargs=None, start_kwargs=None,
            volume_bindings=None, privileged=None):
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

        if 'host_config' not in create_kwargs:
            conf = {}
            if volume_bindings is not None:
                conf['binds'] = volume_bindings

            if privileged is not None:
                conf['privileged'] = privileged

            create_kwargs['host_config'] = self.d.create_host_config(**conf)

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

    def pull_image(self, image, insecure=False, dockercfg_path=None):
        """
        pull provided image from registry

        :param image_name: ImageName, image to pull
        :param insecure: bool, allow connecting to registry over plain http
        :param dockercfg_path: str, path to dockercfg
        :return: str, image (reg.om/img:v1)
        """
        logger.info("pulling image '%s' from registry", image)
        logger.debug("image = '%s', insecure = '%s'", image, insecure)
        tag = image.tag

        if dockercfg_path:
            self.login(registry=image.registry, docker_secret_path=dockercfg_path)
        try:
            command_result = self.retry_generator(self.d.pull,
                                                  image.to_str(tag=False),
                                                  tag=tag, insecure_registry=insecure,
                                                  decode=True, stream=True)
        except TypeError:
            # because changing api is fun
            command_result = self.retry_generator(self.d.pull,
                                                  image.to_str(tag=False),
                                                  tag=tag, decode=True, stream=True)

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

    def login(self, registry, docker_secret_path):
        """
        login to docker registry

        :param registry: registry name
        :param docker_secret_path: path to docker config directory
        """
        logger.info("logging in: registry '%s', secret path '%s'", registry, docker_secret_path)
        # Docker-py needs username
        dockercfg = Dockercfg(docker_secret_path)
        credentials = dockercfg.get_credentials(registry)
        unpacked_auth = dockercfg.unpack_auth_b64(registry)
        username = credentials.get('username')
        if unpacked_auth:
            username = unpacked_auth.username
        if not username:
            raise RuntimeError("Failed to extract a username from '%s'" % dockercfg)

        logger.info("found username %s for registry %s", username, registry)

        response = self.d.login(registry=registry, username=username,
                                dockercfg_path=dockercfg.json_secret_path)
        if not response:
            raise RuntimeError("Failed to login to '%s' with config '%s'" % (registry, dockercfg))
        if u'Status' in response and response[u'Status'] == u'Login Succeeded':
            logger.info("login succeeded")
        else:
            if not(isinstance(response, dict) and 'password' in response.keys()):
                # for some reason docker-py returns the contents of the dockercfg - we shouldn't
                # be displaying that
                logger.debug("response: %r", response)

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
            # push returns string composed of newline separated jsons; exactly what 'docker push'
            # outputs
            command_result = self.retry_generator(self.d.push,
                                                  image.to_str(tag=False),
                                                  tag=image.tag, insecure_registry=insecure,
                                                  decode=True, stream=True)
        except TypeError:
            # because changing api is fun
            command_result = self.retry_generator(self.d.push,
                                                  image.to_str(tag=False),
                                                  tag=image.tag, decode=True, stream=True)

        self.last_logs = command_result.logs
        return command_result.parsed_logs

    def tag_and_push_image(self, image, target_image, insecure=False, force=False,
                           dockercfg=None):
        """
        tag provided image and push it to registry

        :param image: str or ImageName, image id or name
        :param target_image: ImageName, img
        :param insecure: bool, allow connecting to registry over plain http
        :param force: bool, force the tag?
        :param dockercfg: path to docker config
        :return: str, image (reg.com/img:v1)
        """
        logger.info("tagging and pushing image '%s' as '%s'", image, target_image)
        logger.debug("image = '%s', target_image = '%s'", image, target_image)
        self.tag_image(image, target_image, force=force)
        if dockercfg:
            self.login(registry=target_image.registry, docker_secret_path=dockercfg)
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
            if isinstance(response, bytes):
                response = response.decode("utf-8")  # py2 & 3 compat
            response = [line for line in response.splitlines() if line]
        return response

    def wait(self, container_id):
        """
        wait for container to finish the job (may run infinitely)

        :param container_id: str
        :return: int, exit code
        """
        logger.info("waiting for container '%s' to finish", container_id)
        logger.debug("container = '%s'", container_id)
        # docker < 3.0.0 wait methods return int representing the exit code
        # docker >= 3.0.0 wait methods return dict representing the API response
        response = self.d.wait(container_id)
        if isinstance(response, dict):
            response = response['StatusCode']

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
            logger.warning(str(ex))
            response = False
        else:
            response = response is not None
        logger.debug("image exists: %s", response)
        return response

    def get_volumes_for_container(self, container_id, skip_empty_source=True):
        """
        get a list of volumes mounted in a container

        :param container_id: str
        :param skip_empty_source: bool, don't list volumes which are not created on FS
        :return: list, a list of volume names
        """
        def filter_volumes(mount_entry):
            # BW compatibility with older dockerd 'Type' key was not used
            if mount_entry.get('Type', 'volume') == 'volume':
                # Don't show volumes which are not on the filesystem
                if skip_empty_source and not mount_entry['Source']:
                    return False
                return True
            return False

        logger.info("listing volumes for container '%s'", container_id)
        inspect_output = self.d.inspect_container(container_id)
        mounts = inspect_output['Mounts'] or []
        volume_names = [x['Name'] for x in mounts if filter_volumes(x)]

        logger.debug("volumes = %s", volume_names)
        return volume_names

    def remove_volume(self, volume_name):
        """
        remove a volume by its name

        :param volume_name: str
        :return None
        """
        logger.info("removing volume '%s'", volume_name)
        try:
            self.d.remove_volume(volume_name)
        except APIError as ex:
            if ex.response.status_code == requests.codes.CONFLICT:
                logger.debug("ignoring a conflict when removing volume %s", volume_name)
            else:
                raise ex

    def get_image_history(self, image_id):
        """
        return history of image

        :param image_id: str or ImageName, id or name of the image
        :return: dict
        """

        if isinstance(image_id, ImageName):
            image_id = image_id.to_str()
        return self.d.history(image_id)

    def get_image(self, image_id):
        """
        return image content

        :param image_id: str or ImageName, id or name of the image
        :return: dict
        """

        if isinstance(image_id, ImageName):
            image_id = image_id.to_str()
        return self.d.get_image(image_id)

    def import_image_from_stream(self, filesystem):
        """
        return result json from importing image

        :param filesystem: stream
        :return: dict
        """
        return self.d.import_image_from_stream(filesystem)

    def get_archive(self, container_id, dir_path):
        """
        return archive from container

        :param container_id: str
        :param dir_path: str, path from which to get archive
        :return: tuple, First element is a raw tar data stream.
                          Second element is a dict containing stat information
        """
        return self.d.get_archive(container_id, dir_path)

    def export_container(self, container_id):
        """
        return export generator

        :param container_id: str
        :return: str, The filesystem tar archive as a str
        """
        return self.d.export(container_id)

    def create_container(self, image, command):
        """
        create container from provided image

        for more info, see documentation of REST API calls:
         * container/create

        :param image: ImageName or string, name or id of the image
        :param command: str
        :return: dict
        """
        return self.d.create_container(image, command=command)

    def cleanup_containers(self, *container_ids):
        """
        Removes specified containers and their volumes

        :param container_ids: IDs of containers
        """
        volumes = []
        for container_id in container_ids:
            volumes.extend(self.get_volumes_for_container(container_id))

            try:
                self.remove_container(container_id)
            except APIError:
                logger.warning(
                    "error removing container %s (ignored):",
                    container_id, exc_info=True)

        for volume_name in volumes:
            try:
                self.remove_volume(volume_name)
            except APIError:
                logger.warning(
                    "error removing volume %s (ignored):",
                    volume_name, exc_info=True)
