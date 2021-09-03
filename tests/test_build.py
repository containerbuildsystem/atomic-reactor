"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import pytest

import atomic_reactor.util
import docker.errors
from atomic_reactor.build import InsideBuilder, BuildResult
from atomic_reactor.source import get_source_instance_for
from atomic_reactor.util import DockerfileImages, df_parser
from tests.constants import (
    LOCALHOST_REGISTRY, MOCK, SOURCE,
    DOCKERFILE_OK_PATH, DOCKERFILE_MULTISTAGE_PATH,
    DOCKERFILE_MULTISTAGE_SCRATCH_PATH, DOCKERFILE_MULTISTAGE_CUSTOM_PATH,
)
from atomic_reactor.constants import CONTAINER_DOCKERPY_BUILD_METHOD
from osbs.utils import ImageName
from tests.util import requires_internet
from flexmock import flexmock

if MOCK:
    from tests.docker_mock import mock_docker

# This stuff is used in tests; you have to have internet connection,
# running registry on port 5000 and it helps if you've pulled fedora:latest before
git_base_repo = "fedora"
git_base_tag = "latest"
git_base_image = ImageName(registry=LOCALHOST_REGISTRY, repo="fedora", tag="latest")


with_all_sources = pytest.mark.parametrize('source_params', [
    SOURCE,
    {'provider': 'path', 'uri': 'file://' + DOCKERFILE_OK_PATH},
    {'provider': 'path', 'uri': 'file://' + DOCKERFILE_MULTISTAGE_PATH},
    {'provider': 'path', 'uri': 'file://' + DOCKERFILE_MULTISTAGE_SCRATCH_PATH},
    {'provider': 'path', 'uri': 'file://' + DOCKERFILE_MULTISTAGE_CUSTOM_PATH},
])

default_build_method = CONTAINER_DOCKERPY_BUILD_METHOD


@requires_internet
@with_all_sources
def test_inspect_built_image(tmpdir, source_params):
    provided_image = "test-build:test_tag"
    if MOCK:
        mock_docker(provided_image_repotags=provided_image)

    flexmock(InsideBuilder, ensure_is_built=None)
    source_params.update({'tmpdir': str(tmpdir)})
    s = get_source_instance_for(source_params)
    b = InsideBuilder(s, provided_image)
    b.tasker.build_method = default_build_method
    built_inspect = b.inspect_built_image()

    assert built_inspect is not None
    assert built_inspect["Id"] is not None


@requires_internet
@with_all_sources
@pytest.mark.parametrize('insecure', [True, False])
@pytest.mark.parametrize('parents_pulled', [True, False])
def test_parent_image_inspect(insecure, parents_pulled, tmpdir, source_params):
    provided_image = "test-build:test_tag"
    if MOCK:
        mock_docker(provided_image_repotags=provided_image)

    source_params.update({'tmpdir': str(tmpdir)})
    s = get_source_instance_for(source_params)
    b = InsideBuilder(s, provided_image)
    b.tasker.build_method = default_build_method
    b.parents_pulled = parents_pulled

    provided_imagename = ImageName.parse(provided_image)
    registry_name = "registry.example.com"
    provided_imagename.registry = registry_name
    b.pull_registries = {registry_name: {'insecure': insecure, 'dockercfg_path': str(tmpdir)}}

    if not parents_pulled:
        (flexmock(atomic_reactor.util)
         .should_receive('get_inspect_for_image')
         .with_args(provided_imagename, provided_imagename.registry, insecure, str(tmpdir))
         .and_return({'Id': 123}))

    built_inspect = b.parent_image_inspect(provided_imagename)

    assert built_inspect is not None
    assert built_inspect["Id"] is not None


@requires_internet
@with_all_sources
@pytest.mark.parametrize('parents_pulled', [True, False])
@pytest.mark.parametrize('insecure', [True, False])
@pytest.mark.parametrize('base_exist', [True, False])
def test_base_image_inspect(tmpdir, source_params, parents_pulled, insecure, base_exist):
    if MOCK:
        mock_docker()

    source_params.update({'tmpdir': str(tmpdir)})
    s = get_source_instance_for(source_params)
    df_path, _ = s.get_build_file_path()
    dfp = df_parser(df_path)

    b = InsideBuilder(s, '')
    b.dockerfile_images = DockerfileImages(dfp.parent_images)
    b.tasker.build_method = default_build_method
    b.parents_pulled = parents_pulled
    if b.dockerfile_images.base_from_scratch:
        base_exist = True
    registry_name = "registry.example.com"

    original_parents = b.dockerfile_images.original_parents
    new_parents = []
    for parent in original_parents:
        if parent == 'scratch':
            new_parents.append(parent)
        else:
            mod_parent = ImageName.parse(parent)
            mod_parent.registry = registry_name
            new_parents.append(mod_parent.to_str())

    b.dockerfile_images = DockerfileImages(new_parents)
    b.pull_registries = {registry_name: {'insecure': insecure, 'dockercfg_path': str(tmpdir)}}

    if base_exist:
        if b.dockerfile_images.base_from_scratch:
            built_inspect = b.base_image_inspect
            assert built_inspect == {}
        else:
            if not parents_pulled:
                (flexmock(atomic_reactor.util)
                 .should_receive('get_inspect_for_image')
                 .with_args(b.dockerfile_images.base_image, b.dockerfile_images.base_image.registry,
                            insecure, str(tmpdir))
                 .and_return({'Id': 123}))

            built_inspect = b.base_image_inspect

            assert built_inspect is not None
            assert built_inspect["Id"] is not None
    else:
        if parents_pulled or b.dockerfile_images.custom_base_image:
            response = flexmock(content="not found", status_code=404)
            (flexmock(docker.APIClient)
             .should_receive('inspect_image')
             .and_raise(docker.errors.NotFound, "xyz", response))
            with pytest.raises(KeyError):
                b.base_image_inspect    # pylint: disable=pointless-statement; is a property
        else:
            (flexmock(atomic_reactor.util)
             .should_receive('get_inspect_for_image')
             .and_raise(NotImplementedError))
            with pytest.raises(NotImplementedError):
                b.base_image_inspect    # pylint: disable=pointless-statement; is a property


@requires_internet
@with_all_sources
@pytest.mark.parametrize('is_built', [
    True,
    False,
])
def test_ensure_built(tmpdir, source_params, is_built):
    if MOCK:
        mock_docker()

    source_params.update({'tmpdir': str(tmpdir)})
    s = get_source_instance_for(source_params)
    b = InsideBuilder(s, '')
    b.is_built = is_built

    if is_built:
        assert b.ensure_is_built() is None
        with pytest.raises(Exception):
            b.ensure_not_built()
    else:
        assert b.ensure_not_built() is None
        with pytest.raises(Exception):
            b.ensure_is_built()


def test_build_result():
    with pytest.raises(AssertionError):
        BuildResult(fail_reason='it happens', image_id='spam')

    with pytest.raises(AssertionError):
        BuildResult(fail_reason='', image_id='spam')

    with pytest.raises(AssertionError):
        BuildResult(fail_reason='it happens', source_docker_archive='/somewhere')

    with pytest.raises(AssertionError):
        BuildResult(image_id='spam', source_docker_archive='/somewhere')

    with pytest.raises(AssertionError):
        BuildResult(image_id='spam', fail_reason='it happens', source_docker_archive='/somewhere')

    assert BuildResult(fail_reason='it happens').is_failed()
    assert not BuildResult(image_id='spam').is_failed()

    assert BuildResult(image_id='spam', logs=list('logs')).logs == list('logs')

    assert BuildResult(fail_reason='it happens').fail_reason == 'it happens'
    assert BuildResult(image_id='spam').image_id == 'spam'

    assert BuildResult(image_id='spam', annotations={'ham': 'mah'}).annotations == {'ham': 'mah'}

    assert BuildResult(image_id='spam', labels={'ham': 'mah'}).labels == {'ham': 'mah'}

    assert BuildResult(source_docker_archive='/somewhere').source_docker_archive == '/somewhere'

    assert BuildResult(image_id='spam').is_image_available()
    assert not BuildResult(fail_reason='it happens').is_image_available()
    assert not BuildResult.make_remote_image_result().is_image_available()

    assert not BuildResult.make_remote_image_result().is_failed()
