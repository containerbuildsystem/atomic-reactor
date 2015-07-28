"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import json
from atomic_reactor.constants import CONTAINER_RESULTS_JSON_PATH
from atomic_reactor.inner import BuildResultsEncoder
from atomic_reactor.plugin import ExitPlugin


__all__ = ('StoreLogsToFilePlugin', )


class StoreLogsToFilePlugin(ExitPlugin):
    key = "store_logs_to_file"

    def __init__(self, tasker, workflow, file_path):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param file_path: str, path to file where logs should be stored
        """
        # call parent constructor
        super(StoreLogsToFilePlugin, self).__init__(tasker, workflow)
        self.file_path = file_path

    def run(self):
        file_path = self.file_path or CONTAINER_RESULTS_JSON_PATH
        results = {
            'prebuild_plugins': self.workflow.prebuild_results,
            'postbuild_plugins': self.workflow.postbuild_results,
        }

        with open(file_path, 'w') as results_json_fd:
            json.dump(results, results_json_fd, cls=BuildResultsEncoder)
