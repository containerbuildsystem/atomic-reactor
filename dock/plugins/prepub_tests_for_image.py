"""
Plugin which runs arbitrary test suite.

The test suite is executed from provided git repo (git_uri). You have to provide image ID,
this image will be tested. Python source file with tests is loaded from the git repo, specify
name or name it tests.py. Tests accept configuration file config_file (default value is
config.json). You can also specify arbitrary keyword arguments which will be passed to the
test module.

The test module has to satisfy two conditions:

1. it has to have function with prototype

    def run(config_file, image_id, logger=None, **kwargs):
        ...
        return results, passed

2. it has to return two values:

    1. first value is results
    2. second value is bool, whether test suite passed

third optional argument of run function is logger (if not specified, all logs are 'print'ed to stdout)
"""

import os
import imp

from dock.plugin import PrePublishPlugin
from dock.util import LazyGit


class ImageTestPlugin(PrePublishPlugin):
    key = "test_built_image"

    def __init__(self, tasker, workflow, git_uri, image_id, tests_git_path="tests.py",
                 config_file="config.json", **kwargs):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockurBuildWorkflow instance
        :param git_uri: str, URI to git repo (URL, path -- this is passed to 'git clone')
        :param image_id: str, ID of image to process
        :param tests_git_path: str, relative path within git repo to file with tests (default=tests.py)
        :param config_file: str, relative path within git to config file for tests (default=config.json)
        :param kwargs: dict, additional arguments for tests
        """
        # call parent constructor
        super(ImageTestPlugin, self).__init__(tasker, workflow)
        self.git_uri = git_uri
        self.image_id = image_id
        self.tests_git_path = tests_git_path
        self.config_file = config_file
        self.kwargs = kwargs

    def run(self):
        if not self.image_id:
            self.log.warning("no image_id specified (build probably failed)")
            return
        g = LazyGit(git_url=self.git_uri)
        with g:
            tests_file = os.path.abspath(os.path.join(g.git_path, self.tests_git_path))
            self.log.debug("loading file with tests: '%s'", tests_file)
            module_name, module_ext = os.path.splitext(self.tests_git_path)
            tests_module = imp.load_source(module_name, tests_file)

            orig_path = os.getcwd()
            os.chdir(g.git_path)
            results, passed = tests_module.run(config_file=self.config_file, image_id=self.image_id,
                                               logger=self.log, **self.kwargs)
            os.chdir(orig_path)
            if not passed:
                self.log.error("tests failed: %s", results)
                raise RuntimeError("Tests didn't pass!")
            return results
