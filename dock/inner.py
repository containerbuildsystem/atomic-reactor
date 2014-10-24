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

    def build_docker_image(self):
        """
        build docker image

        :return:
        """
        db = DockerBuilder(self.git_url, self.local_tag, self.git_dockerfile_path, self.git_commit, self.repos)
        if self.parent_registry:
            db.pull_base_image(self.parent_registry)

        logs_generator = db.build()
        logs = list(logs_generator)
        #logger.debug("image_id = '%s'", image_id)
        logger.debug("LOGS\n%s", logs)
        if self.store_results:
            db.push_built_image('172.17.42.1:5000')
        if self.target_registries:
            for target_registry in self.target_registries:
                db.push_built_image(target_registry, self.tag)

        return self.local_tag


if __name__ == '__main__':
    with open(CONTAINER_BUILD_JSON_PATH, 'r') as build_json_fd:
        # TODO: validate json
        build_json = json.load(build_json_fd)
    dbw = DockerBuildWorkflow(**build_json)
    dbw.build_docker_image()
