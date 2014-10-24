import json

from dock import CONTAINER_BUILD_JSON_PATH
from dock.inner import DockerBuildWorkflow


def build():
    with open(CONTAINER_BUILD_JSON_PATH, 'r') as build_json_fd:
        # TODO: validate json
        build_json = json.load(build_json_fd)
    dbw = DockerBuildWorkflow(**build_json)
    dbw.build_docker_image()


def run():
    build()


if __name__ == '__main__':
    build()