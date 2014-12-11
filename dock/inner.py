"""
Script for building docker image. This is expected to run inside container.
"""

import json
import logging
import os
import shutil
import tempfile

from dock.constants import CONTAINER_BUILD_JSON_PATH, CONTAINER_RESULTS_JSON_PATH
from dock.build import InsideBuilder
from dock.plugin import PostBuildPluginsRunner


logger = logging.getLogger(__name__)


class BuildResults(object):
    build_logs = None
    dockerfile = None
    built_img_inspect = None
    built_img_info = None
    base_img_inspect = None
    base_img_info = None
    base_plugins_output = None
    built_img_plugins_output = None
    container_id = None


class BuildResultsEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, BuildResults):
            return {
                'build_logs': obj.build_logs,
                'built_img_inspect': obj.built_img_inspect,
                'built_img_info': obj.built_img_info,
                'base_img_info': obj.base_img_info,
                'base_plugins_output': obj.base_plugins_output,
                'built_img_plugins_output': obj.built_img_plugins_output,
            }
        # Let the base class default method raise the TypeError
        return json.JSONEncoder.default(self, obj)


class BuildResultsJSONDecoder(json.JSONDecoder):
    def decode(self, obj):
        d = super(BuildResultsJSONDecoder, self).decode(obj)
        results = BuildResults()
        results.built_img_inspect = d.get('built_img_inspect', None)
        results.built_img_info = d.get('built_img_info', None)
        results.base_img_info = d.get('base_img_info', None)
        results.base_plugins_output = d.get('base_plugins_output', None)
        results.built_img_plugins_output = d.get('built_img_plugins_output', None)
        return results


class DockerBuildWorkflow(object):
    """
    This class defines a workflow for building images:

    1. pull image from registry
    2. tag it properly if needed
    3. clone git repo
    4. build image
    5. tag it
    6. push it to registries
    """

    def __init__(self, git_url, image, git_dockerfile_path=None,
                 git_commit=None, parent_registry=None, target_registries=None,
                 repos=None):
        """
        :param git_url: str, URL to git repo
        :param image: str, tag for built image ([registry/]image_name[:tag])
        :param git_dockerfile_path: str, path to dockerfile within git repo (if not in root)
        :param git_commit: str, git commit to check out
        :param parent_registry: str, registry to pull base image from
        :param target_registries: list of str, list of registries to push image to (might change in future)
        :param repos: override package manager repositories (not implemented)
        """
        self.git_url = git_url
        self.image = image
        self.git_dockerfile_path = git_dockerfile_path
        self.git_commit = git_commit
        self.parent_registry = parent_registry
        self.target_registries = target_registries
        self.repos = repos
        self.db = None

    def build_docker_image(self, copy_df_to_share_dir=False):
        """
        build docker image

        :return: BuildResults
        """
        tmpdir = tempfile.mkdtemp()
        self.db = InsideBuilder(self.git_url, self.image, git_dockerfile_path=self.git_dockerfile_path,
                                git_commit=self.git_commit, repos=self.repos, tmpdir=tmpdir)
        try:
            if self.parent_registry:
                self.db.pull_base_image(self.parent_registry)

            image = self.db.build(copy_df_to_share_dir=copy_df_to_share_dir)
            # TODO: in case of docker host build, remove image
            build_logs = self.db.last_logs
            if self.target_registries:
                for target_registry in self.target_registries:
                    self.db.push_built_image(target_registry)

            results = self._prepare_response()
            results.build_logs = build_logs
            return results
        finally:
            shutil.rmtree(tmpdir)

    def _prepare_response(self):
        """
        prepare response for build: gather info about images

        :return BuildResults
        """
        assert self.db is not None
        runner = PostBuildPluginsRunner(self.db.tasker)
        results = BuildResults()
        results.built_img_inspect = self.db.inspect_built_image()
        results.built_img_info = self.db.get_built_image_info()
        results.base_img_inspect = self.db.inspect_base_image()
        results.base_img_info = self.db.get_base_image_info()
        results.base_plugins_output = runner.run(self.db.base_image_name)
        results.built_img_plugins_output = runner.run(self.db.image)
        return results


def get_build_config_from_path(path=None):
    """
    get json with build config from environment variable
    """
    path = path or CONTAINER_BUILD_JSON_PATH
    try:
        with open(path, 'r') as build_cfg_fd:
            build_cfg_json = json.load(build_cfg_fd)
    except ValueError:
        logger.error("couldn't decode json from file '%s'", path)
        return None
    except IOError:
        logger.error("couldn't read json from file '%s'", path)
        return None
    else:
        return build_cfg_json


def get_build_config_from_env(env_name=None):
    """
    get json with build config from environment variable
    """
    env_name = env_name or "BUILD_CONFIG"
    try:
        build_cfg_json = os.environ[env_name]
    except KeyError:
        logger.info("build config was not specified via environment variable")
        return None
    else:
        return json.loads(build_cfg_json)


def build_inside():
    """
    load configuration from CONTAINER_BUILD_JSON_PATH, build image
    from the provided conf and store results to CONTAINER_RESULTS_JSON_PATH
    """
    # env > file
    build_json = get_build_config_from_env()
    if not build_json:
        build_json = get_build_config_from_path()
    if not build_json:
        raise RuntimeError("No valid build json!")
    # TODO: validate json
    dbw = DockerBuildWorkflow(**build_json)
    results = dbw.build_docker_image(copy_df_to_share_dir=True)
    with open(CONTAINER_RESULTS_JSON_PATH, 'w') as results_json_fd:
        json.dump(results, results_json_fd, cls=BuildResultsEncoder)
