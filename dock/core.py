import re
import os
import shutil
import logging
import tempfile
import datetime

import git
import docker
from docker.errors import APIError
from dock import CONTAINER_DOCKERFILE_PATH


DOCKER_SOCKET_PATH = '/var/run/docker.sock'

logger = logging.getLogger(__name__)


def split_image_repo_name(image_name):
    """ registry.com/image -> (registry, image) """
    result = image_name.split('/', 1)
    if len(result) == 1:
        return [""] + result
    else:
        return result


def create_image_repo_name(image_name, registry):
    """ (image_name, registry) -> "registry/image_name" """
    if not registry.endswith('/'):
        registry += '/'
    return registry + image_name

def get_baseimage_from_dockerfile_path(path):
    with open(path, 'r') as dockerfile:
        for line in dockerfile:
            if line.startswith("FROM"):
                return line.split()[1]

def get_baseimage_from_dockerfile(url, path=None):
    """ return name of base image from provided gitrepo """
    temp_dir = tempfile.mkdtemp()
    try:
        git.Repo.clone_from(url, temp_dir)
        # lets be naive for now
        if path:
            dockerfile_path = os.path.join(temp_dir, path, 'Dockerfile')
        else:
            dockerfile_path = os.path.join(temp_dir, 'Dockerfile')
        return get_baseimage_from_dockerfile_path(dockerfile_path)
    finally:
        shutil.rmtree(temp_dir)


class DockerTasker(object):
    def __init__(self):
        self.d = docker.Client(base_url='unix:/%s' % DOCKER_SOCKET_PATH, version='1.12', timeout=30)

    def build_image_dockerhost(self, build_image, url, tag):
        """
        Build docker image within a build image using docker from host (mount docker socket inside container).
        There are possible races here. Use wisely.

        :param build_image:
        :param url:
        :param tag:
        :return:
        """
        print "build_image: build_image = '%s', url = '%s', tag = '%s'" % (build_image, url, tag)
        container_dict = self.d.create_container(
            build_image,
            environment={
                "DOCKER_CONTEXT_URL": url,
                "BUILD_TAG": tag,
            },
            volumes=[DOCKER_SOCKET_PATH]
        )
        container_id = container_dict['Id']

        volume_bindings = {
            DOCKER_SOCKET_PATH: {
                'bind': DOCKER_SOCKET_PATH,
                'ro': True,
            }
        }
        response = self.d.start(
            container_id,
            binds=volume_bindings,
        )
        print "response = '%s'" % response
        return container_id

    def build_image(self, tag, path, git_path=None):
        temp_dir = tempfile.mkdtemp()
        response = None
        try:
            git.Repo.clone_from(path, temp_dir)
            if git_path:
                if git_path.endswith('Dockerfile'):
                    git_df_dir = os.path.dirname(git_path)
                    df_path = os.path.abspath(os.path.join(temp_dir, git_df_dir))
                else:
                    df_path = os.path.abspath(os.path.join(temp_dir, git_path))
            else:
                df_path = temp_dir
            shutil.copyfile(os.path.join(df_path, "Dockerfile"), CONTAINER_DOCKERFILE_PATH)
            logger.debug("build (git): tag = '%s', path = '%s'", tag, df_path)
            base_image = get_baseimage_from_dockerfile_path(os.path.join(df_path, "Dockerfile"))
            response = self.d.build(path=df_path, tag=tag)  # returns generator
        finally:
            try:
                shutil.rmtree(temp_dir)
            except (IOError, OSError) as ex:
                # no idea why this's happening
                logger.warning("Failed to remove dir '%s': '%s'", temp_dir, repr(ex))
        logger.debug("build finished")
        return response, base_image

    def run(self, image_id, command=None, create_kwargs=None, start_kwargs=None):
        logger.debug("run: image = '%s', command = '%s'", image_id, command)
        if create_kwargs:
            container_dict = self.d.create_container(image_id, command=command, **create_kwargs)
        else:
            container_dict = self.d.create_container(image_id, command=command)
        container_id = container_dict['Id']
        logger.debug("container_id = '%s'", container_id)
        if start_kwargs:
            self.d.start(container_id, **start_kwargs)  # returns None
        else:
            self.d.start(container_id)
        return container_id

    def commit_container(self, container_id, message):
        print "commit: id = '%s', message = '%s'" % (container_id, message)
        response = self.d.commit(container_id, message=message)
        print "response = %s" % response
        return response['Id']

    def get_image_info(self, image_id=None, name=None):
        """
        using `docker images` provide information about an image

        :param image_id: hash of image to get info
        :param name: image name ([repository/][namespace/]image_name:tag)
        :return: dict or None
        """
        logger.debug("get image info: image_id = '%s', name = '%s'", image_id, name)
        if not image_id and not name:
            raise RuntimeError("you have to specify either name or image_id")
        # returns list of
        # {u'Created': 1414577076,
        #  u'Id': u'3ab9a7ed8a169ab89b09fb3e12a14a390d3c662703b65b4541c0c7bde0ee97eb',
        #  u'ParentId': u'a79ad4dac406fcf85b9c7315fe08de5b620c1f7a12f45c8185c843f4b4a49c4e',
        #  u'RepoTags': [u'buildroot-fedora:latest'],
        #  u'Size': 0,
        #  u'VirtualSize': 856564160}
        if name:
            name = name.split(":")[0]  # if there is version in here, docker doesnt output anything
            try:
                return self.d.images(name=name)[0]
            except IndexError:
                return None
        else:
            images = self.d.images()
            try:
                image_dict = [i for i in images if i['Id'] == image_id][0]
            except IndexError:
                return None
            else:
                return image_dict

    def pull_image(self, image, registry):
        """ pull image from registry """
        logger.debug("pull: image = '%s', registry = '%s'", image, registry)
        registry_uri = create_image_repo_name(image, registry)
        try:
            logs_gen = self.d.pull(registry_uri, insecure_registry=True, stream=True)
        except TypeError:
            # because changing api is fun
            logs_gen = self.d.pull(registry_uri, stream=True)
        while True:
            try:
                logger.debug(logs_gen.next())  # wait for pull to finish
                # send logs to server
            except StopIteration:
                break
        return registry_uri

    def tag_image(self, image, tag, registry=None, version=None):
        """ tag image with provided tag """
        final_tag = tag
        if registry:
            final_tag = create_image_repo_name(tag, registry)
        print self.d.tag(image, final_tag, tag=version)
        return final_tag

    def tag_and_push_image(self, image, tag, registry, version=None):
        """ tag and push specified image to registry """
        logger.debug("tag&push: image = '%s', tag = '%s', registry = '%s'", image, tag, registry)
        final_tag = self.tag_image(image, tag, registry=registry, version=version)
        try:
            self.d.push(final_tag, insecure_registry=True)  # prints shitload of stuff
        except TypeError:
            # because changing api is fun
            self.d.push(final_tag)

    def inspect_image(self, image_id):
        """ return json with detailed information about image """
        return self.d.inspect_image(image_id)

    def remove_image(self, image_id):
        return self.d.remove_image(image_id)

    def stdout_of_container(self, container_id, stream=True):
        print 'stdout: container = %s' % container_id
        if stream:
            stream = self.d.logs(container_id, stdout=True, stderr=True, stream=stream)
            response = list(stream)
        else:
            response = self.d.logs(container_id, stdout=True, stderr=True, stream=stream)
            response = [line for line in response.split('\n') if line]
        return response

    def wait(self, container_id):
        logger.debug("wait: container = '%s'", container_id)
        response = self.d.wait(container_id)
        logger.debug("response = '%s'", response)
        return response

    def image_exists(self, image_id):
        try:
            return self.d.get_image(image_id) is not None
        except APIError:
            return False


class DockerBuilder(object):
    """
    extremely simple state machine for building docker images

    state is controlled with variable 'is_built'

    """
    def __init__(self, git_url, local_tag, git_dockerfile_path=None, git_commit=None, repos=None):
        self.tasker = DockerTasker()

        # arguments for build
        self.git_url = git_url
        self.base_image = None
        self.local_tag = local_tag
        self.git_dockerfile_path = git_dockerfile_path
        self.git_commit = git_commit
        self.repos = repos

        # build artefacts
        self.build_container_id = None
        self.build_image_id = None
        self.build_image_tag = None
        self.buildimage_version = None

        self.is_built = False

    def pull_base_image(self, source_registry):
        """ pull base image

        :param source_registry: registry to pull from
        :return:
        """
        logger.debug("pull base image")
        assert not self.is_built
        self.base_image = get_baseimage_from_dockerfile(self.git_url)
        logger.debug("base image = '%s'", self.base_image)
        df_registry, base_image_name = split_image_repo_name(self.base_image)
        if df_registry:
            if df_registry != source_registry:
                raise RuntimeError(
                    "Registry specified in dockerfile doesn't match provided one. Dockerfile: %s, Provided: %s"
                    % (df_registry, source_registry))
        self.tasker.pull_image(self.base_image, source_registry)
        # FIXME: this fails
        # if not self.base_image.startswith(source_registry):
        #    self.tasker.tag_image(self.base_image, base_image_name)

    def build(self):
        """
        build image inside current environment
        :return:
        """
        logger.debug("build")
        assert not self.is_built
        logs_gen, self.base_image = self.tasker.build_image(
            self.local_tag,
            self.git_url,
            git_path=self.git_dockerfile_path,
        )
        self.is_built = True
        logger.debug("waiting for build to finish...")
        # you have to wait for logs_gen to raise StopIter so you know the build has finished
        while True:
            try:
                logger.debug(logs_gen.next())  # wait for build to finish
                # send logs to server
            except StopIteration:
                break
        logger.debug("base_image = '%s'", self.base_image)
        return logs_gen

    def build_hostdocker(self, build_image):
        """
        build image inside build image using host's docker

        TODO: this should be part of different class

        :param build_image:
        :return:
        """
        assert not self.is_built
        self.build_container_id = self.tasker.build_image_dockerhost(
            build_image,
            self.git_url,
            self.local_tag
        )
        self.is_built = True
        # save the time when image was built
        self.buildimage_version = datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
        if self.base_image:
            self.tasker.remove_image(self.base_image)
        commit_message = "docker build of '%s' (%s)" % (self.local_tag, self.git_url)
        self.build_image_tag = "buildroot-%s" % self.local_tag
        self.build_image_id = self.tasker.commit_container(
            self.build_container_id, commit_message)

    def push_buildroot(self, registry):
        # FIXME: this should be part of different class, since it is related to dockerhost method
        assert self.is_built
        self.tasker.tag_and_push_image(
            self.build_image_id,
            self.build_image_tag,
            registry=registry,
            version=self.buildimage_version)

    def push_built_image(self, registry, tag=None):
        assert self.is_built
        self.tasker.tag_and_push_image(self.local_tag,
                                       tag or self.local_tag,
                                       registry)

    def inspect_built_image(self):
        assert self.is_built
        inspect_data = self.tasker.inspect_image(self.local_tag)  # dict with lots of data, see man docker-inspect
        return inspect_data

    def get_base_image_info(self):
        assert self.is_built
        image_info = self.tasker.get_image_info(name=self.base_image)
        return image_info

    def get_built_image_info(self):
        assert self.is_built
        image_info = self.tasker.get_image_info(name=self.local_tag)
        return image_info

    def run_postbuild_plugins(self, *plugins):
        assert self.is_built
        result = {}
        for plugin_const in plugins:
            plugin = get_postbuild_plugin(plugin_const)
            result[plugin.name] = plugin.run()
        return result

