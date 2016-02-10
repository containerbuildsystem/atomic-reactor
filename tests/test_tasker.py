"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

from tests.fixtures import temp_image_name

from atomic_reactor.core import DockerTasker
from atomic_reactor.util import ImageName, clone_git_repo
from tests.constants import LOCALHOST_REGISTRY, INPUT_IMAGE, DOCKERFILE_GIT, MOCK, COMMAND
from tests.util import requires_internet

import docker, docker.errors

from flexmock import flexmock
import pytest

if MOCK:
    from tests.docker_mock import mock_docker

input_image_name = ImageName.parse(INPUT_IMAGE)

# TEST-SUITE SETUP

def setup_module(module):
    if MOCK:
        return
    d = docker.Client()
    try:
        d.inspect_image(INPUT_IMAGE)
        setattr(module, 'HAS_IMAGE', True)
    except docker.errors.APIError:
        _ = [x for x in d.pull(INPUT_IMAGE, stream=True)]
        setattr(module, 'HAS_IMAGE', False)


def teardown_module(module):
    if MOCK:
        return
    if not getattr(module, 'HAS_IMAGE', False):
        d = docker.Client()
        d.remove_image(INPUT_IMAGE)


# TESTS

def test_run():
    if MOCK:
        mock_docker()

    t = DockerTasker()
    container_id = t.run(input_image_name, command="id")
    try:
        t.wait(container_id)
    finally:
        t.remove_container(container_id)


def test_run_invalid_command():
    if MOCK:
        mock_docker(should_raise_error={'start': []})

    t = DockerTasker()
    try:
        with pytest.raises(docker.errors.APIError):
            t.run(input_image_name, command=COMMAND)
    finally:
        # remove the container
        containers = t.d.containers(all=True)
        container_id = [c for c in containers if c["Command"] == COMMAND][0]['Id']
        t.remove_container(container_id)


def test_image_exists():
    if MOCK:
        mock_docker()

    t = DockerTasker()
    assert t.image_exists(input_image_name) is True


def test_image_doesnt_exist():
    image = "lerknglekrnglekrnglekrnglekrng"
    if MOCK:
        mock_docker(should_raise_error={'inspect_image': [image]})

    t = DockerTasker()
    assert t.image_exists(image) is False


def test_logs():
    if MOCK:
        mock_docker()

    t = DockerTasker()
    container_id = t.run(input_image_name, command="id")
    try:
        t.wait(container_id)
        output = t.logs(container_id, stderr=True, stream=False)
        assert "\n".join(output).startswith("uid=")
    finally:
        t.remove_container(container_id)


def test_remove_container():
    if MOCK:
        mock_docker()

    t = DockerTasker()
    container_id = t.run(input_image_name, command="id")
    try:
        t.wait(container_id)
    finally:
        t.remove_container(container_id)


def test_remove_image(temp_image_name):
    if MOCK:
        mock_docker(inspect_should_fail=True)

    t = DockerTasker()
    container_id = t.run(input_image_name, command="id")
    t.wait(container_id)
    image_id = t.commit_container(container_id, image=temp_image_name)
    try:
        t.remove_container(container_id)
    finally:
        t.remove_image(image_id)
    assert not t.image_exists(temp_image_name)


def test_commit_container(temp_image_name):
    if MOCK:
        mock_docker()

    t = DockerTasker()
    container_id = t.run(INPUT_IMAGE, command="id")
    t.wait(container_id)
    image_id = t.commit_container(container_id, message="test message", image=temp_image_name)
    try:
        assert t.image_exists(image_id)
    finally:
        t.remove_container(container_id)
        t.remove_image(image_id)


def test_inspect_image():
    if MOCK:
        mock_docker()

    t = DockerTasker()
    inspect_data = t.inspect_image(input_image_name)
    assert isinstance(inspect_data, dict)


def test_tag_image(temp_image_name):
    if MOCK:
        mock_docker()

    t = DockerTasker()
    temp_image_name.registry = "somewhere.example.com"
    temp_image_name.tag = "1"
    img = t.tag_image(INPUT_IMAGE, temp_image_name)
    try:
        assert t.image_exists(temp_image_name)
        assert img == temp_image_name.to_str()
    finally:
        t.remove_image(temp_image_name)


def test_tag_image_same_name(temp_image_name):
    if MOCK:
        mock_docker()

    t = DockerTasker()
    temp_image_name.registry = "somewhere.example.com"
    temp_image_name.tag = "1"
    flexmock(t.d).should_receive('tag').never()
    img = t.tag_image(temp_image_name, temp_image_name.copy())


@pytest.mark.parametrize(('should_fail',), [
    (True, ),
    (False, ),
])
def test_push_image(temp_image_name, should_fail):
    if MOCK:
        mock_docker(push_should_fail=should_fail)

    t = DockerTasker()
    temp_image_name.registry = LOCALHOST_REGISTRY
    temp_image_name.tag = "1"
    t.tag_image(INPUT_IMAGE, temp_image_name)
    if should_fail:
        with pytest.raises(RuntimeError) as exc:
            output = t.push_image(temp_image_name, insecure=True)
        assert "Failed to push image" in str(exc)
    else:
        output = t.push_image(temp_image_name, insecure=True)
        assert output is not None
    t.remove_image(temp_image_name)


def test_tag_and_push(temp_image_name):
    if MOCK:
        mock_docker()

    t = DockerTasker()
    temp_image_name.registry = LOCALHOST_REGISTRY
    temp_image_name.tag = "1"
    output = t.tag_and_push_image(INPUT_IMAGE, temp_image_name, insecure=True)
    assert output is not None
    assert t.image_exists(temp_image_name)
    t.remove_image(temp_image_name)


def test_pull_image():
    if MOCK:
        mock_docker()

    t = DockerTasker()
    local_img = input_image_name
    remote_img = local_img.copy()
    remote_img.registry = LOCALHOST_REGISTRY
    t.tag_and_push_image(local_img, remote_img, insecure=True)
    got_image = t.pull_image(remote_img, insecure=True)
    assert remote_img.to_str() == got_image
    assert len(t.last_logs) > 0
    t.remove_image(remote_img)


def test_get_image_info_by_id_nonexistent():
    if MOCK:
        mock_docker()

    t = DockerTasker()
    response = t.get_image_info_by_image_id("asd")
    assert response is None


def test_get_image_info_by_id():
    if MOCK:
        mock_docker(provided_image_repotags=input_image_name.to_str())

    t = DockerTasker()
    image_id = t.get_image_info_by_image_name(input_image_name)[0]['Id']
    response = t.get_image_info_by_image_id(image_id)
    assert isinstance(response, dict)


def test_get_image_info_by_name_tag_in_name():
    if MOCK:
        mock_docker()

    t = DockerTasker()
    response = t.get_image_info_by_image_name(input_image_name)
    assert len(response) == 1


def test_get_image_info_by_name_tag_in_name_nonexisten(temp_image_name):
    if MOCK:
        mock_docker()

    t = DockerTasker()
    response = t.get_image_info_by_image_name(temp_image_name)
    assert len(response) == 0


@requires_internet
def test_build_image_from_path(tmpdir, temp_image_name):
    if MOCK:
        mock_docker()

    tmpdir_path = str(tmpdir.realpath())
    clone_git_repo(DOCKERFILE_GIT, tmpdir_path)
    df = tmpdir.join("Dockerfile")
    assert df.check()
    t = DockerTasker()
    response = t.build_image_from_path(tmpdir_path, temp_image_name, use_cache=True)
    list(response)
    assert response is not None
    assert t.image_exists(temp_image_name)
    t.remove_image(temp_image_name)


@requires_internet
def test_build_image_from_git(temp_image_name):
    if MOCK:
        mock_docker()

    t = DockerTasker()
    response = t.build_image_from_git(DOCKERFILE_GIT, temp_image_name, use_cache=True)
    list(response)
    assert response is not None
    assert t.image_exists(temp_image_name)
    t.remove_image(temp_image_name)


def test_get_info():
    if MOCK:
        mock_docker()

    t = DockerTasker()
    response = t.get_info()
    assert isinstance(response, dict)


def test_get_version():
    if MOCK:
        mock_docker()

    t = DockerTasker()
    response = t.get_info()
    assert isinstance(response, dict)


@pytest.mark.parametrize(('timeout', 'expected_timeout'), [
    (None, 120),
    (60, 60),
])
def test_timeout(timeout, expected_timeout):
    (flexmock(docker.Client)
        .should_receive('__init__')
        .with_args(version=str, timeout=expected_timeout))

    kwargs = {}
    if timeout is not None:
        kwargs['timeout'] = timeout

    t = DockerTasker(**kwargs)
