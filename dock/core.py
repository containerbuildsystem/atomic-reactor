import re
import os
import shutil
import logging
import tempfile
import datetime

import git
import docker


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
        with open(dockerfile_path, 'r') as dockerfile:
            for line in dockerfile:
                if line.startswith("FROM"):
                    return line.split()[1]
    finally:
        shutil.rmtree(temp_dir)


class PostBuildPlugin(object):
    def __init__(self):
        """ """


class PostBuildRPMqaPlugin(object):
    def __init__(self):
        """ """

    @property
    def key(self):
        return "all_packages"

    @property
    def command(self):
        """ command to run in image """
        return "/bin/rpm -qa"


def run_postbuild_plugins(dt, image):
    """ dt = instance of dockertasker """
    # FIXME: load all class which subclass PostBuildPlugin
    p = PostBuildRPMqaPlugin()
    container_id = dt.run(image, p.command)
    result = dt.stdout_of_container(container_id)
    response_dict = {p.key: result}
    print response_dict
    return response_dict


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
        try:
            git.Repo.clone_from(path, temp_dir)
            if git_path:
                if git_path.endswith('Dockerfile'):
                    git_df_dir = os.path.dirname(git_path)
                    df_path = os.path.join(temp_dir, git_df_dir)
                else:
                    df_path = os.path.join(temp_dir, git_path)
            else:
                df_path = temp_dir
            logger.debug("build (git): tag = '%s', path = '%s'", tag, df_path)
            response = self.d.build(path=df_path, tag=tag)  # returns generator
        finally:
            shutil.rmtree(temp_dir)
        logger.debug("build finished")
        return response

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

    def pull_image(self, image, registry):
        """ pull image from registry """
        print "pull: image = '%s', registry = '%s'" % (image, registry)
        registry_uri = create_image_repo_name(image, registry)
        print self.d.pull(registry_uri, insecure_registry=True)
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
        print "tag&push: image = '%s', tag = '%s', registry = '%s'" % (image, tag, registry)
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

    def stdout_of_container(self, container_id):
        print 'stdout: container = %s' % container_id
        stream = self.d.logs(container_id, stdout=True, stderr=True, stream=True)
        response = list(stream)
        print response
        return response

    def wait(self, container_id):
        logger.debug("wait: container = '%s'", container_id)
        response = self.d.wait(container_id)
        logger.debug("response = '%s'", response)
        return response


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

        :param image_id: tag or image id of image to pull
        :param source_registry: registry to pull from
        :return:
        """
        assert not self.is_built
        base_image = get_baseimage_from_dockerfile(self.git_url)
        if not base_image.startswith(source_registry):
            df_registry, base_image_name = split_image_repo_name(base_image)
            if df_registry:
                if df_registry != source_registry:
                    raise RuntimeError(
                        "Registry specified in dockerfile doesn't match provided one. Dockerfile: %s, Provided: %s"
                        % (df_registry, source_registry))
            self.tasker.pull_image(base_image, source_registry)
            self.tasker.tag_image(base_image, base_image_name)

    def build(self):
        """
        build image inside current environment
        :return:
        """
        logger.debug("build")
        assert not self.is_built
        response = self.tasker.build_image(
            self.local_tag,
            self.git_url,
            git_path=self.git_dockerfile_path,
        )
        self.is_built = True
        logger.debug("response = '%s'", response)
        return response

    def build_hostdocker(self, build_image):
        """
        build image inside build image using host's docker

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

    def run_postbuild_plugins(self, *plugins):
        assert self.is_built
        result = {}
        for plugin_const in plugins:
            plugin = get_postbuild_plugin(plugin_const)
            result[plugin.name] = plugin.run()
        return result

