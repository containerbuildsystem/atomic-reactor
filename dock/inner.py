"""
Script for building docker image. This is expected to run inside container.
"""

import logging
import json
from dock import CONTAINER_BUILD_JSON_PATH
from dock.core import DockerBuilder

logger = logging.getLogger(__name__)


class DockerBuildWorkflow(object):
    """
    This class defines a workflow for building images:

    TBD
    """

    def __init__(self, git_url, local_tag, git_dockerfile_path=None,
                 git_commit=None, parent_registry=None, target_registries=None,
                 tag=None, repos=None, store_results=True):
        self.git_url = git_url
        self.local_tag = local_tag
        self.git_dockerfile_path = git_dockerfile_path
        self.git_commit = git_commit
        self.parent_registry = parent_registry
        self.target_registries = target_registries
        self.tag = tag
        self.repos = repos
        self.store_results = store_results
        self.db = None

    def build_docker_image(self):
        """
        build docker image

        :return:
        """
        self.db = DockerBuilder(self.git_url, self.local_tag, self.git_dockerfile_path, self.git_commit, self.repos)
        if self.parent_registry:
            self.db.pull_base_image(self.parent_registry)

        logs_generator = self.db.build()  # this produces _a lot_ of logs
        logger.debug("logs\n%s", list(logs_generator))
        logger.debug("images = '%s'", self.db.tasker.d.images())
        if self.store_results:
            # XXX: hardcoded
            self.db.push_built_image('172.17.42.1:5000')  # docker's network
        if self.target_registries:
            for target_registry in self.target_registries:
                self.db.push_built_image(target_registry, self.tag)

        response = self._prepare_response()
        logger.debug("response = '%s'", response)
        return response

    def _prepare_response(self):
        assert self.db is not None
        response = {
            'built_img_inspect': self.db.inspect_built_image(),
            'built_img_info': self.db.get_built_image_info(),
            'base_img_info': self.db.get_base_image_info(),
        }
        return response


if __name__ == '__main__':
    with open(CONTAINER_BUILD_JSON_PATH, 'r') as build_json_fd:
        # TODO: validate json
        build_json = json.load(build_json_fd)
    dbw = DockerBuildWorkflow(**build_json)
    dbw.build_docker_image()
