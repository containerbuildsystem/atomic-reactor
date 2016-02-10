"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals

import os
import pytest

from atomic_reactor.build import InsideBuilder
from atomic_reactor.core import DockerTasker
from atomic_reactor.source import get_source_instance_for
from atomic_reactor.util import ImageName
from tests.constants import LOCALHOST_REGISTRY, DOCKERFILE_GIT, DOCKERFILE_OK_PATH,\
        DOCKERFILE_ERROR_BUILD_PATH, MOCK, SOURCE, DOCKERFILE_FILENAME
from tests.util import requires_internet

if MOCK:
    from tests.docker_mock import mock_docker

# This stuff is used in tests; you have to have internet connection,
# running registry on port 5000 and it helps if you've pulled fedora:latest before
git_base_repo = "fedora"
git_base_tag = "latest"
git_base_image = ImageName(registry=LOCALHOST_REGISTRY, repo="fedora", tag="latest")


with_all_sources = pytest.mark.parametrize('source_params', [
    SOURCE,
    {'provider': 'path', 'uri': 'file://' + DOCKERFILE_OK_PATH}
])


@requires_internet
@with_all_sources
def test_build_image(tmpdir, source_params):
    provided_image = "test-build:test_tag"
    if MOCK:
        mock_docker(provided_image_repotags=provided_image)

    source_params.update({'tmpdir': str(tmpdir)})
    s = get_source_instance_for(source_params)
    t = DockerTasker()
    b = InsideBuilder(s, provided_image)
    build_result = b.build()
    assert t.inspect_image(build_result.image_id)
    # clean
    t.remove_image(build_result.image_id)

@requires_internet
@pytest.mark.parametrize('source_params', [
    {'provider': 'git', 'uri': DOCKERFILE_GIT, 'provider_params': {'git_commit': 'error-build'}},
    {'provider': 'path', 'uri': 'file://' + DOCKERFILE_ERROR_BUILD_PATH},
])
def test_build_bad_git_commit_dockerfile(tmpdir, source_params):
    provided_image = "test-build:test_tag"
    if MOCK:
        mock_docker(build_should_fail=True, provided_image_repotags=provided_image)

    source_params.update({'tmpdir': str(tmpdir)})
    s = get_source_instance_for(source_params)
    b = InsideBuilder(s, provided_image)
    build_result = b.build()
    assert build_result.is_failed()


@requires_internet
@with_all_sources
def test_inspect_built_image(tmpdir, source_params):
    provided_image = "test-build:test_tag"
    if MOCK:
        mock_docker(provided_image_repotags=provided_image)

    source_params.update({'tmpdir': str(tmpdir)})
    s = get_source_instance_for(source_params)
    t = DockerTasker()
    b = InsideBuilder(s, provided_image)
    build_result = b.build()
    built_inspect = b.inspect_built_image()

    assert built_inspect is not None
    assert built_inspect["Id"] is not None

    # clean
    t.remove_image(build_result.image_id)


@requires_internet
@with_all_sources
def test_inspect_base_image(tmpdir, source_params):
    if MOCK:
        mock_docker()

    source_params.update({'tmpdir': str(tmpdir)})
    s = get_source_instance_for(source_params)
    b = InsideBuilder(s, '')
    built_inspect = b.inspect_base_image()

    assert built_inspect is not None
    assert built_inspect["Id"] is not None


@requires_internet
@with_all_sources
def test_get_base_image_info(tmpdir, source_params):
    if MOCK:
        mock_docker(provided_image_repotags='fedora:latest')

    source_params.update({'tmpdir': str(tmpdir)})
    s = get_source_instance_for(source_params)
    b = InsideBuilder(s, '')
    built_inspect = b.get_base_image_info()

    assert built_inspect is not None
    assert built_inspect["Id"] is not None
    assert built_inspect["RepoTags"] is not None
