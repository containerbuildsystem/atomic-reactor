"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from dock.build import InsideBuilder
from dock.core import DockerTasker
from dock.source import GitSource
from dock.util import ImageName
from tests.constants import LOCALHOST_REGISTRY, DOCKERFILE_GIT, MOCK

if MOCK:
    from tests.docker_mock import mock_docker

# This stuff is used in tests; you have to have internet connection,
# running registry on port 5000 and it helps if you've pulled fedora:latest before
git_base_repo = "fedora"
git_base_tag = "latest"
git_base_image = ImageName(registry=LOCALHOST_REGISTRY, repo="fedora", tag="latest")


def test_pull_base_image(tmpdir):
    if MOCK:
        mock_docker()

    s = GitSource(provider='git', uri=DOCKERFILE_GIT)
    t = DockerTasker()
    b = InsideBuilder(s, "", tmpdir=str(tmpdir))
    reg_img_name = b.pull_base_image(LOCALHOST_REGISTRY, insecure=True)
    reg_img_name = ImageName.parse(reg_img_name)
    assert t.inspect_image(reg_img_name) is not None
    assert reg_img_name.repo == git_base_image.repo
    assert reg_img_name.tag == git_base_image.tag
    # clean
    t.remove_image(git_base_image)


def test_build_image(tmpdir):
    provided_image = "test-build:test_tag"
    if MOCK:
        mock_docker(provided_image_repotags=provided_image)

    s = GitSource(provider='git', uri=DOCKERFILE_GIT)
    t = DockerTasker()
    b = InsideBuilder(s, provided_image, tmpdir=str(tmpdir))
    build_result = b.build()
    assert t.inspect_image(build_result.image_id)
    # clean
    t.remove_image(build_result.image_id)


def test_build_error_dockerfile(tmpdir):
    provided_image = "test-build:test_tag"
    if MOCK:
        mock_docker(build_should_fail=True, provided_image_repotags=provided_image)

    s = GitSource(provider='git', uri=DOCKERFILE_GIT,
            provider_params={'git_commit': 'error-build'})
    b = InsideBuilder(s, provided_image, tmpdir=str(tmpdir))
    build_result = b.build()
    assert build_result.is_failed()


def test_inspect_built_image(tmpdir):
    provided_image = "test-build:test_tag"
    if MOCK:
        mock_docker(provided_image_repotags=provided_image)

    s = GitSource(provider='git', uri=DOCKERFILE_GIT)
    t = DockerTasker()
    b = InsideBuilder(s, provided_image, tmpdir=str(tmpdir))
    build_result = b.build()
    built_inspect = b.inspect_built_image()

    assert built_inspect is not None
    assert built_inspect["Id"] is not None

    # clean
    t.remove_image(build_result.image_id)


def test_inspect_base_image(tmpdir):
    if MOCK:
        mock_docker()

    s = GitSource(provider='git', uri=DOCKERFILE_GIT)
    b = InsideBuilder(s, '', tmpdir=str(tmpdir))
    built_inspect = b.inspect_base_image()

    assert built_inspect is not None
    assert built_inspect["Id"] is not None


def test_get_base_image_info(tmpdir):
    if MOCK:
        mock_docker(provided_image_repotags='fedora:latest')

    s = GitSource(provider='git', uri=DOCKERFILE_GIT)
    b = InsideBuilder(s, '', tmpdir=str(tmpdir))
    built_inspect = b.get_base_image_info()

    assert built_inspect is not None
    assert built_inspect["Id"] is not None
    assert built_inspect["RepoTags"] is not None
