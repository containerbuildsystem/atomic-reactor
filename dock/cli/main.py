import json

from dock import CONTAINER_BUILD_JSON_PATH, CONTAINER_RESULTS_JSON_PATH
from dock.inner import DockerBuildWorkflow


def build():
    with open(CONTAINER_BUILD_JSON_PATH, 'r') as build_json_fd:
        # TODO: validate json
        build_json = json.load(build_json_fd)
    dbw = DockerBuildWorkflow(**build_json)
    return dbw.build_docker_image()


def store_result(results):
    # TODO: move this to api, it shouldnt be part of CLI
    with open(CONTAINER_RESULTS_JSON_PATH, 'w') as results_json_fd:
        json.dump(results, results_json_fd)


def run():
    results = build()
    store_result(results)


if __name__ == '__main__':
    build()