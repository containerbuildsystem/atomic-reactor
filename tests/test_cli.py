"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import logging
import os
import sys

import pytest

from atomic_reactor.buildimage import BuildImageBuilder
from atomic_reactor.core import DockerTasker
import atomic_reactor.cli.main

from tests.fixtures import is_registry_running, temp_image_name, get_uuid
from tests.constants import LOCALHOST_REGISTRY, DOCKERFILE_GIT, DOCKERFILE_OK_PATH, FILES, MOCK

if MOCK:
    from tests.docker_mock import mock_docker

PRIV_BUILD_IMAGE = None
DH_BUILD_IMAGE = None


logger = logging.getLogger('atomic_reactor.tests')

if MOCK:
    mock_docker()
dt = DockerTasker()
reactor_root = os.path.dirname(os.path.dirname(__file__))

with_all_sources = pytest.mark.parametrize('source_provider, uri', [
    ('git', DOCKERFILE_GIT),
    ('path', DOCKERFILE_OK_PATH),
])

# TEST-SUITE SETUP

def setup_module(module):
    global PRIV_BUILD_IMAGE, DH_BUILD_IMAGE
    PRIV_BUILD_IMAGE = get_uuid()
    DH_BUILD_IMAGE = get_uuid()
    if MOCK:
        return

    b = BuildImageBuilder(reactor_local_path=reactor_root)
    b.create_image(os.path.join(reactor_root, 'images', 'privileged-builder'),
                   PRIV_BUILD_IMAGE, use_cache=True)

    b2 = BuildImageBuilder(reactor_local_path=reactor_root)
    b2.create_image(os.path.join(reactor_root, 'images', 'dockerhost-builder'),
                    DH_BUILD_IMAGE, use_cache=True)


def teardown_module(module):
    if MOCK:
        return
    dt.remove_image(PRIV_BUILD_IMAGE, force=True)
    dt.remove_image(DH_BUILD_IMAGE, force=True)


# TESTS

class TestCLISuite(object):

    def exec_cli(self, command):
        saved_args = sys.argv
        sys.argv = command
        atomic_reactor.cli.main.run()
        sys.argv = saved_args

    @with_all_sources
    def test_simple_privileged_build(self, is_registry_running, temp_image_name,
            source_provider, uri):
        if MOCK:
            mock_docker()

        temp_image = temp_image_name
        command = [
            "main.py",
            "--verbose",
            "build",
            source_provider,
            "--method", "privileged",
            "--build-image", PRIV_BUILD_IMAGE,
            "--image", temp_image.to_str(),
            "--uri", uri,
        ]
        if is_registry_running:
            logger.info("registry is running")
            command += ["--source-registry", LOCALHOST_REGISTRY]
        else:
            logger.info("registry is NOT running")
        with pytest.raises(SystemExit) as excinfo:
            self.exec_cli(command)

        assert excinfo.value.code == 0

    @with_all_sources
    def test_simple_dh_build(self, is_registry_running, temp_image_name, source_provider, uri):
        if MOCK:
            mock_docker()

        temp_image = temp_image_name
        command = [
            "main.py",
            "--verbose",
            "build",
            source_provider,
            "--method", "hostdocker",
            "--build-image", DH_BUILD_IMAGE,
            "--image", temp_image.to_str(),
            "--uri", uri,
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

    def test_building_from_json_source_provider(self, is_registry_running, temp_image_name):
        if MOCK:
            mock_docker()

        temp_image = temp_image_name
        command = [
            "main.py",
            "--verbose",
            "build",
            "json",
            "--method", "hostdocker",
            "--build-image", DH_BUILD_IMAGE,
            os.path.join(FILES, 'example-build.json'),
            "--substitute", "image={0}".format(temp_image),
            "source.uri={0}".format(DOCKERFILE_OK_PATH)
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
        if MOCK:
            mock_docker()

        temp_image = temp_image_name
        priv_builder_path = os.path.join(reactor_root, 'images', 'privileged-builder')
        command = [
            "main.py",
            "--verbose",
            "create-build-image",
            "--reactor-local-path", reactor_root,
            priv_builder_path,
            temp_image.to_str(),
        ]
        with pytest.raises(SystemExit) as excinfo:
            self.exec_cli(command)
        assert excinfo.value.code == 0
        dt.remove_image(temp_image, noprune=True)
