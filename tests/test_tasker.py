"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals, absolute_import


from atomic_reactor.core import DockerTasker, retry, RetryGeneratorException
from atomic_reactor.util import ImageName, clone_git_repo, CommandResult
from tests.constants import LOCALHOST_REGISTRY, INPUT_IMAGE, DOCKERFILE_GIT, MOCK, COMMAND
from requests.packages.urllib3.exceptions import ProtocolError, ReadTimeoutError
from tests.util import requires_internet

from base64 import b64encode
import json
import os
import docker
import docker.errors
import requests
import sys
import time
import atomic_reactor
from docker.errors import APIError

from flexmock import flexmock
import pytest

if MOCK:
    from tests.docker_mock import mock_docker

input_image_name = ImageName.parse(INPUT_IMAGE)

# TEST-SUITE SETUP


def setup_module(module):
    if MOCK:
        return
    d = docker.Client()  # TODO: current version of python-docker does not have Client anymore
    try:
        d.inspect_image(INPUT_IMAGE)
        setattr(module, 'HAS_IMAGE', True)
    except docker.errors.APIError:
        d.pull(INPUT_IMAGE)
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


def test_remove_image(temp_image_name, docker_tasker):  # noqa
    if MOCK:
        mock_docker(inspect_should_fail=True)

    container_id = docker_tasker.run(input_image_name, command="id")
    docker_tasker.wait(container_id)
    image_id = docker_tasker.commit_container(container_id, image=temp_image_name)
    try:
        docker_tasker.remove_container(container_id)
    finally:
        docker_tasker.remove_image(image_id)
    assert not docker_tasker.image_exists(temp_image_name)


def test_commit_container(temp_image_name):  # noqa
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


def test_tag_image(temp_image_name):  # noqa
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


def test_tag_image_same_name(temp_image_name):  # noqa
    if MOCK:
        mock_docker()

    t = DockerTasker()
    temp_image_name.registry = "somewhere.example.com"
    temp_image_name.tag = "1"

    flexmock(docker.APIClient).should_receive('tag').never()
    t.tag_image(temp_image_name, temp_image_name.copy())


@pytest.mark.parametrize(('should_fail',), [  # noqa
    (True, ),
    (False, ),
])
def test_push_image(temp_image_name, should_fail):
    if MOCK:
        mock_docker(push_should_fail=should_fail)

    t = DockerTasker(retry_times=0)
    temp_image_name.registry = LOCALHOST_REGISTRY
    temp_image_name.tag = "1"
    t.tag_image(INPUT_IMAGE, temp_image_name)
    if should_fail:
        with pytest.raises(RetryGeneratorException) as exc:
            output = t.push_image(temp_image_name, insecure=True)
        assert "Failed to mock_method image" in str(exc)
        assert "connection refused" in str(exc)
    else:
        output = t.push_image(temp_image_name, insecure=True)
        assert output is not None
    t.remove_image(temp_image_name)


def test_tag_and_push(temp_image_name):  # noqa
    if MOCK:
        mock_docker()

    t = DockerTasker()
    temp_image_name.registry = LOCALHOST_REGISTRY
    temp_image_name.tag = "1"
    output = t.tag_and_push_image(INPUT_IMAGE, temp_image_name, insecure=True)
    assert output is not None
    assert t.image_exists(temp_image_name)
    t.remove_image(temp_image_name)


@pytest.mark.parametrize(('insecure', 'dockercfg'), [
    (True, None),
    (False, None),
    (False, {LOCALHOST_REGISTRY: {"auth": b64encode(b'user:mypassword').decode('utf-8')}}),
])
def test_pull_image(tmpdir, insecure, dockercfg):
    if MOCK:
        mock_docker()

    dockercfg_path = None
    if dockercfg:
        dockercfg_path = str(tmpdir.realpath())
        file_name = '.dockercfg'
        dockercfg_secret_path = os.path.join(dockercfg_path, file_name)
        with open(dockercfg_secret_path, "w+") as dockerconfig:
            dockerconfig.write(json.dumps(dockercfg))
            dockerconfig.flush()

    t = DockerTasker()
    local_img = input_image_name
    remote_img = local_img.copy()
    remote_img.registry = LOCALHOST_REGISTRY
    t.tag_and_push_image(local_img, remote_img, insecure=insecure, dockercfg=dockercfg_path)
    got_image = t.pull_image(remote_img, insecure=insecure, dockercfg_path=dockercfg_path)
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


def test_get_image_info_by_name_tag_in_name_nonexisten(temp_image_name):  # noqa
    if MOCK:
        mock_docker()

    t = DockerTasker()
    response = t.get_image_info_by_image_name(temp_image_name)
    assert len(response) == 0


@requires_internet  # noqa
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


@requires_internet  # noqa
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


@pytest.mark.parametrize('no_container', (False, True))
def test_export(no_container):
    if MOCK:
        mock_docker()

    t = DockerTasker()
    container_dict = t.d.create_container(INPUT_IMAGE, command=["/bin/bash"])
    container_id = container_dict['Id']

    try:
        if no_container:
            with pytest.raises(docker.errors.APIError):
                t.d.export('NOT_THERE')
        else:
            export_generator = t.d.export(container_id)
            for _ in export_generator:
                pass
    finally:
        t.d.remove_container(container_id)


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
    if not hasattr(docker, 'APIClient'):
        setattr(docker, 'APIClient', docker.Client)

    expected_kwargs = {
        'timeout': expected_timeout,
    }
    if hasattr(docker, 'AutoVersionClient'):
        expected_kwargs['version'] = 'auto'

    (flexmock(docker.APIClient)
        .should_receive('__init__')
        .with_args(**expected_kwargs))

    kwargs = {}
    if timeout is not None:
        kwargs['timeout'] = timeout

    DockerTasker(**kwargs)


def test_docker2():
    class MockClient(object):
        def __init__(self, **kwargs):
            pass

        def version(self):
            return {}

    for client in ['APIClient', 'Client']:
        if not hasattr(docker, client):
            setattr(docker, client, MockClient)

    (flexmock(docker)
        .should_receive('APIClient')
        .once()
        .and_raise(AttributeError))

    (flexmock(docker)
        .should_receive('Client')
        .once())

    DockerTasker()


def my_func(*args, **kwargs):
    my_args = ('some', 'new')
    my_kwargs = {'one': 'first', 'two': 'second'}
    assert args == my_args
    assert kwargs == my_kwargs
    response = requests.Response()
    response.status_code = 408
    raise APIError("test fail", response)


@pytest.mark.parametrize('retry_times', [-1, 0, 1, 2, 3])
def test_retry_method(retry_times):
    my_args = ('some', 'new')
    my_kwargs = {'one': 'first', 'two': 'second'}

    (flexmock(sys.modules[__name__])
        .should_call('my_func')
        .with_args(*my_args, **my_kwargs)
        .times(retry_times+1))
    (flexmock(time)
        .should_receive('sleep')
        .and_return(None))

    if retry_times >= 0:
        with pytest.raises(docker.errors.APIError):
            retry(my_func, *my_args, retry=retry_times, **my_kwargs)
    else:
        retry(my_func, *my_args, retry=retry_times, **my_kwargs)


@pytest.mark.parametrize('exc', [ProtocolError, APIError, ReadTimeoutError, False])
@pytest.mark.parametrize('in_init', [True, False])
@pytest.mark.parametrize('retry_times', [-1, 0, 1, 2, 3])
def test_retry_generator(exc, in_init, retry_times):
    def simplegen():
        yield "log line"

    my_args = ('some', 'new')
    if not in_init:
        my_kwargs = {'one': 'first', 'two': 'second', 'retry_times': retry_times}
    else:
        my_kwargs = {'one': 'first', 'two': 'second'}

    if in_init:
        t = DockerTasker(retry_times=retry_times)
    else:
        t = DockerTasker()

    (flexmock(time)
        .should_receive('sleep')
        .and_return(None))

    if not exc:
        cr = CommandResult()
        cr._error = "cmd_error"
        cr._error_detail = {"message": "error_detail"}

    if exc == APIError:
        error_message = 'api_error'
        response = flexmock(content=error_message, status_code=408)
        (flexmock(atomic_reactor.util)
            .should_receive('wait_for_command')
            .times(retry_times + 1)
            .and_raise(APIError, error_message, response))
    elif exc == ProtocolError:
        error_message = 'protocol_error'
        (flexmock(atomic_reactor.util)
            .should_receive('wait_for_command')
            .times(retry_times + 1)
            .and_raise(ProtocolError, error_message))
    elif exc == ReadTimeoutError:
        pool = 'pool'
        message = 'read_timeout_error'
        error_message = '{}: {}'.format(pool, message)
        (flexmock(atomic_reactor.util)
            .should_receive('wait_for_command')
            .times(retry_times + 1)
            .and_raise(ReadTimeoutError, pool, 'url', message))
    else:
        (flexmock(atomic_reactor.util)
            .should_receive('wait_for_command')
            .times(retry_times + 1)
            .and_return(cr))
        error_message = 'cmd_error'

    if retry_times >= 0:
        with pytest.raises(RetryGeneratorException) as ex:
            t.retry_generator(lambda *args, **kwargs: simplegen(),
                              *my_args, **my_kwargs)

        assert repr(error_message) in repr(ex.value)
    else:
        t.retry_generator(lambda *args, **kwargs: simplegen(),
                          *my_args, **my_kwargs)


@pytest.mark.parametrize(("dockerconfig_contents", "should_raise"), [
    ({LOCALHOST_REGISTRY: {"foo": "bar"}}, True),
    ({LOCALHOST_REGISTRY: {"auth": b64encode(b'user').decode('utf-8')}}, True),
    ({LOCALHOST_REGISTRY: {"auth": b64encode(b'user:mypassword').decode('utf-8')}}, False),
    ({LOCALHOST_REGISTRY: {"username": "user", "password": "mypassword"}}, False)])
def test_login(tmpdir, dockerconfig_contents, should_raise):
    if MOCK:
        mock_docker()
        fake_api = flexmock(docker.APIClient, login=lambda username, registry,
                            dockercfg_path: {'Status': 'Login Succeeded'})

    tmpdir_path = str(tmpdir.realpath())
    file_name = '.dockercfg'
    dockercfg_path = os.path.join(tmpdir_path, file_name)
    with open(dockercfg_path, "w+") as dockerconfig:
        dockerconfig.write(json.dumps(dockerconfig_contents))
        dockerconfig.flush()
    t = DockerTasker()
    if should_raise:
        if 'auth' in dockerconfig_contents[LOCALHOST_REGISTRY]:
            with pytest.raises(ValueError) as exc:
                t.login(LOCALHOST_REGISTRY, tmpdir_path)
                assert "Failed to parse 'auth'" in str(exc)
        else:
            with pytest.raises(RuntimeError) as exc:
                t.login(LOCALHOST_REGISTRY, tmpdir_path)
                assert "Failed to extract a username" in str(exc)
    else:
        if MOCK:
            (fake_api
             .should_receive('login')
             .with_args(username='user', registry=LOCALHOST_REGISTRY, dockercfg_path=dockercfg_path)
             .once().and_return({'Status': 'Login Succeeded'}))
        t.login(LOCALHOST_REGISTRY, tmpdir_path)
