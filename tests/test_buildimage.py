"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from glob import glob
import os

from dock.buildimage import BuildImageBuilder
from dock.core import DockerTasker

from tests.constants import MOCK

if MOCK:
    from tests.docker_mock import mock_docker

PARENT_DIR = os.path.dirname(os.path.dirname(__file__))
TEST_BUILD_IMAGE = "test-build-image"


def test_tarball_generation_local_repo(tmpdir):
    b = BuildImageBuilder(dock_local_path=PARENT_DIR)
    tarball_path = b.get_dock_tarball_path(str(tmpdir))
    assert os.path.exists(tarball_path)
    assert len(glob(os.path.join(str(tmpdir), 'dock-*.tar.gz'))) == 1


def test_tarball_generation_upstream_repo(tmpdir):
    b = BuildImageBuilder(use_official_dock_git=True)
    tarball_path = b.get_dock_tarball_path(str(tmpdir))
    assert os.path.exists(tarball_path)
    assert len(glob(os.path.join(str(tmpdir), 'dock-*.tar.gz'))) == 1


def test_image_creation_upstream_repo():
    if MOCK:
        mock_docker()

    b = BuildImageBuilder(use_official_dock_git=True)
    df_dir_path = os.path.join(PARENT_DIR, 'images', 'privileged-builder')
    b.create_image(df_dir_path, TEST_BUILD_IMAGE)

    dt = DockerTasker()
    assert dt.image_exists(TEST_BUILD_IMAGE)
    dt.remove_image(TEST_BUILD_IMAGE)


def test_image_creation_local_repo():
    if MOCK:
        mock_docker()

    b = BuildImageBuilder(dock_local_path=PARENT_DIR)
    df_dir_path = os.path.join(PARENT_DIR, 'images', 'privileged-builder')
    b.create_image(df_dir_path, TEST_BUILD_IMAGE)

    dt = DockerTasker()
    assert dt.image_exists(TEST_BUILD_IMAGE)
    dt.remove_image(TEST_BUILD_IMAGE)