from __future__ import print_function, unicode_literals

import logging
import os
import sys

import pytest

from dock.buildimage import BuildImageBuilder
from dock.core import DockerTasker
import dock.cli.main

from fixtures import is_registry_running, temp_image_name, get_uuid
from constants import LOCALHOST_REGISTRY, DOCKERFILE_GIT


PRIV_BUILD_IMAGE = None
DH_BUILD_IMAGE = None


logger = logging.getLogger('dock.tests')
dt = DockerTasker()
dock_root = os.path.dirname(os.path.dirname(__file__))


# TEST-SUITE SETUP

def setup_module(module):
    global PRIV_BUILD_IMAGE, DH_BUILD_IMAGE
    PRIV_BUILD_IMAGE = get_uuid()
    DH_BUILD_IMAGE = get_uuid()

    b = BuildImageBuilder(dock_local_path=dock_root)
    b.create_image(os.path.join(dock_root, 'images', 'privileged-builder'),
                   PRIV_BUILD_IMAGE, use_cache=True)

    b2 = BuildImageBuilder(dock_local_path=dock_root)
    b2.create_image(os.path.join(dock_root, 'images', 'dockerhost-builder'),
                    DH_BUILD_IMAGE, use_cache=True)


def teardown_module(module):
    dt.remove_image(PRIV_BUILD_IMAGE, force=True)
    dt.remove_image(DH_BUILD_IMAGE, force=True)


# TESTS

class TestCLISuite(object):

    def exec_cli(self, command):
        saved_args = sys.argv
        sys.argv = command
        dock.cli.main.run()
        sys.argv = saved_args

    def test_simple_privileged_build(self, is_registry_running, temp_image_name):
        temp_image = temp_image_name
        command = [
            "main.py",
            "-v",
            "build",
            "--method", "privileged",
            "--build-image", PRIV_BUILD_IMAGE,
            "--image", temp_image,
            "--git-url", DOCKERFILE_GIT,
        ]
        if is_registry_running:
            logger.info("registry is running")
            command += ["--source-registry", LOCALHOST_REGISTRY]
        else:
            logger.info("registry is NOT running")
        with pytest.raises(SystemExit) as excinfo:
            self.exec_cli(command)
        assert excinfo.value.code == 0

    def test_simple_dh_build(self, is_registry_running, temp_image_name):
        temp_image = temp_image_name
        command = [
            "main.py",
            "-v",
            "build",
            "--method", "hostdocker",
            "--build-image", DH_BUILD_IMAGE,
            "--image", temp_image,
            "--git-url", DOCKERFILE_GIT,
        ]
        if is_registry_running:
            logger.info("registry is running")
            command += ["--source-registry", LOCALHOST_REGISTRY]
        else:
            logger.info("registry is NOT running")
        with pytest.raises(SystemExit) as excinfo:
            self.exec_cli(command)
        assert excinfo.value.code == 0
        dt.remove_image(temp_image, noprune=True)

    def test_create_build_image(self, temp_image_name):
        temp_image = temp_image_name
        priv_builder_path = os.path.join(dock_root, 'images', 'privileged-builder')
        command = [
            "main.py",
            "-v",
            "create-build-image",
            "--dock-local-path", dock_root,
            priv_builder_path,
            temp_image,
        ]
        with pytest.raises(SystemExit) as excinfo:
            self.exec_cli(command)
        assert excinfo.value.code == 0
        dt.remove_image(temp_image, noprune=True)
