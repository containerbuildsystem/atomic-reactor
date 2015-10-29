"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


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
import shutil
import tempfile


from atomic_reactor.plugin import PrePublishPlugin
from atomic_reactor.util import LazyGit


class ImageTestPlugin(PrePublishPlugin):
    key = "test_built_image"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, git_uri, git_commit, image_id, tests_git_path="tests.py",
                 tests = None, results_dir="results", **kwargs):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param git_uri: str, URI to git repo (URL, path -- this is passed to 'git clone')
        :param image_id: str, ID of image to process
        :param tests_git_path: str, relative path within git repo to file with tests (default=tests.py)
        :param config_file: str, relative path within git to config file for tests (default=config.json)
        :param kwargs: dict, additional arguments for tests
        """
        # call parent constructor
        super(ImageTestPlugin, self).__init__(tasker, workflow)
        self.git_uri = git_uri
        self.git_commit = git_commit
        self.image_id = image_id
        self.tests_git_path = tests_git_path
        self.tests = tests
        self.results_dir = results_dir
        self.kwargs = kwargs

    def run(self):
        """
        this method will:
        1) clone git repository with test files into temp location
        2) execute tests
        3) clear repository

        """
        if not self.image_id:
            raise RuntimeError("no image_id specified (build probably failed)")
        tmpdir = tempfile.mkdtemp()
        g = LazyGit(self.git_uri, self.git_commit, tmpdir)
        with g:
            tests_file = os.path.abspath(os.path.join(g.git_path, self.tests_git_path))
            self.log.debug("loading file with tests: '%s'", tests_file)
            module_name, dummy_module_ext = os.path.splitext(self.tests_git_path)
            tests_module = imp.load_source(module_name, tests_file)

            results, passed = tests_module.run(image_id=self.image_id, tests=self.tests,
                                               git_repo_path = tmpdir, logger=self.log,
                                               results_dir=self.results_dir, **self.kwargs)

        shutil.rmtree(tmpdir)
        if not passed:
            self.log.error("tests failed: %s", results)
            raise RuntimeError("Tests didn't pass!")
        return results
