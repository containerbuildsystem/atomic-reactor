import json
import sys
import os
from dock.constants import CONTAINER_RESULTS_JSON_PATH
from dock.inner import BuildResultsEncoder
from dock.plugin import PrePublishPlugin


__all__ = ('StoreLogsToFilePlugin', )


class RunTestForContainer(PrePublishPlugin):
    key = "run_test_for_container"
    image = None
    path = None
    class_name = None
    git_repo = None

    def __init__(self, tasker, workflow, test_class_name, image_dir, git_repo_path, image_id):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param file_path: str, path to file where logs should be stored
        """
        # call parent constructor
        super(RunTestForContainer, self).__init__(tasker, workflow)
        self.image_id = image_id
        self.image_dir = image_dir
        self.class_name = test_class_name
        self.git_repo = git_repo_path

    def run(self):
        if self.image_dir is None:
            dir = self.git_repo + "/"
        else:
            dir = self.git_repo + "/" + self.image_dir + "/"
        self.exec_test_from_dir(dir)

    def exec_test_from_dir(self, dir):
        dir = os.path.abspath(dir)
        sys.path.insert(0, dir)
        module = __import__("mw_docker_smoke_tests")
        obj = getattr(module, self.class_name)
        test = obj()
        test.setup(image=self.image, config_file=dir+'/config.json' )
        test.run()
        test.teardown()
