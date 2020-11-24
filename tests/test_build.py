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
from atomic_reactor.util import df_parser, DockerfileImages
from tests.constants import (
    LOCALHOST_REGISTRY, MOCK, SOURCE,
    DOCKERFILE_OK_PATH, DOCKERFILE_MULTISTAGE_PATH,
    DOCKERFILE_MULTISTAGE_SCRATCH_PATH, DOCKERFILE_MULTISTAGE_CUSTOM_PATH,
    DOCKERFILE_MULTISTAGE_CUSTOM_BAD_PATH
)
from atomic_reactor.constants import CONTAINER_DOCKERPY_BUILD_METHOD
from osbs.utils import ImageName
from tests.util import requires_internet
from flexmock import flexmock
from textwrap import dedent

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
def test_different_custom_base_images(tmpdir):
    if MOCK:
        mock_docker()
    source_params = {'provider': 'path', 'uri': 'file://' + DOCKERFILE_MULTISTAGE_CUSTOM_BAD_PATH,
                     'tmpdir': str(tmpdir)}
    s = get_source_instance_for(source_params)
    with pytest.raises(NotImplementedError) as exc:
        InsideBuilder(s, '')
    message = "multiple different custom base images aren't allowed in Dockerfile"
    assert message in str(exc.value)


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
    b = InsideBuilder(s, '')
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
@pytest.mark.parametrize(('image', 'will_raise'), [
    (
        "buildroot-fedora:latest",
        False,
    ),
    (
        "non-existing",
        True,
    ),
])
def test_get_base_image_info(tmpdir, source_params, image, will_raise):
    if DOCKERFILE_MULTISTAGE_CUSTOM_PATH in source_params['uri']:
        return
    if MOCK:
        mock_docker(provided_image_repotags=image)

    source_params.update({'tmpdir': str(tmpdir)})
    s = get_source_instance_for(source_params)
    b = InsideBuilder(s, image)
    b.tasker.build_method = default_build_method
    if b.dockerfile_images.base_from_scratch:
        will_raise = False

    if will_raise:
        with pytest.raises(Exception):
            b.get_base_image_info()
    else:
        built_inspect = b.get_base_image_info()
        if b.dockerfile_images.base_from_scratch:
            assert built_inspect is None
        else:
            assert built_inspect is not None
            assert built_inspect["Id"] is not None
            assert built_inspect["RepoTags"] is not None


def test_no_base_image(tmpdir):
    if MOCK:
        mock_docker()

    source = {'provider': 'path', 'uri': 'file://' + DOCKERFILE_OK_PATH, 'tmpdir': str(tmpdir)}
    b = InsideBuilder(get_source_instance_for(source), 'built-img')
    dfp = df_parser(str(tmpdir))
    dfp.content = "# no FROM\nADD spam /eggs"
    with pytest.raises(RuntimeError) as exc:
        b.set_df_path(str(tmpdir))
    assert "no base image specified" in str(exc.value)


def test_copy_from_is_blocked(tmpdir):
    """test when user has specified COPY --from=image (instead of builder)"""
    dfp = df_parser(str(tmpdir))
    if MOCK:
        mock_docker()
    source = {'provider': 'path', 'uri': 'file://' + str(tmpdir), 'tmpdir': str(tmpdir)}

    dfp.content = dedent("""\
        FROM monty AS vikings
        FROM python
        COPY --from=vikings /spam/eggs /bin/eggs
        COPY --from=0 /spam/eggs /bin/eggs
        COPY src dest
    """)
    # init calls set_df_path, which should not raise an error:
    InsideBuilder(get_source_instance_for(source), 'built-img')

    dfp.content = dedent("""\
        FROM monty as vikings
        FROM python
        # using a stage name we haven't seen should break:
        COPY --from=notvikings /spam/eggs /bin/eggs
    """)
    with pytest.raises(RuntimeError) as exc_info:
        InsideBuilder(get_source_instance_for(source), 'built-img')  # calls set_df_path at init
    assert "FROM notvikings AS source" in str(exc_info.value)

    dfp.content = dedent("""\
        FROM monty as vikings
        # using an index we haven't seen should break:
        COPY --from=5 /spam/eggs /bin/eggs
    """)
    with pytest.raises(RuntimeError) as exc_info:
        InsideBuilder(get_source_instance_for(source), 'built-img')  # calls set_df_path at init
    assert "COPY --from=5" in str(exc_info.value)


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


@requires_internet
@with_all_sources
@pytest.mark.parametrize(('image', 'will_raise'), [
    (
        "buildroot-fedora:latest",
        False,
    ),
    (
        "non-existing",
        True,
    ),
])
def test_get_image_built_info(tmpdir, source_params, image, will_raise):
    if MOCK:
        mock_docker(provided_image_repotags=image)

    source_params.update({'tmpdir': str(tmpdir)})
    s = get_source_instance_for(source_params)
    b = InsideBuilder(s, image)
    b.tasker.build_method = default_build_method

    if will_raise:
        with pytest.raises(Exception):
            b.get_built_image_info()
    else:
        b.get_built_image_info()


def test_build_result():
    with pytest.raises(AssertionError):
        BuildResult(fail_reason='it happens', image_id='spam')

    with pytest.raises(AssertionError):
        BuildResult(fail_reason='', image_id='spam')

    with pytest.raises(AssertionError):
        BuildResult(fail_reason='it happens', oci_image_path='/somewhere')

    with pytest.raises(AssertionError):
        BuildResult(image_id='spam', oci_image_path='/somewhere')

    with pytest.raises(AssertionError):
        BuildResult(image_id='spam', fail_reason='it happens', oci_image_path='/somewhere')

    assert BuildResult(fail_reason='it happens').is_failed()
    assert not BuildResult(image_id='spam').is_failed()

    assert BuildResult(image_id='spam', logs=list('logs')).logs == list('logs')

    assert BuildResult(fail_reason='it happens').fail_reason == 'it happens'
    assert BuildResult(image_id='spam').image_id == 'spam'

    assert BuildResult(image_id='spam', annotations={'ham': 'mah'}).annotations == {'ham': 'mah'}

    assert BuildResult(image_id='spam', labels={'ham': 'mah'}).labels == {'ham': 'mah'}

    assert BuildResult(oci_image_path='/somewhere').oci_image_path == '/somewhere'

    assert BuildResult(image_id='spam').is_image_available()
    assert not BuildResult(fail_reason='it happens').is_image_available()
    assert not BuildResult.make_remote_image_result().is_image_available()

    assert not BuildResult.make_remote_image_result().is_failed()


def test_parent_images_to_str(tmpdir, caplog):
    if MOCK:
        mock_docker()

    source = {'provider': 'path', 'uri': 'file://' + DOCKERFILE_OK_PATH, 'tmpdir': str(tmpdir)}
    b = InsideBuilder(get_source_instance_for(source), 'built-img')
    b.dockerfile_images = DockerfileImages(['fedora:latest', 'bacon'])
    b.dockerfile_images['fedora:latest'] = "spam"
    expected_results = {
        "fedora:latest": "spam:latest"
    }
    assert b.parent_images_to_str() == expected_results
    assert "None in: base bacon:latest has parent None" in caplog.text
