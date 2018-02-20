"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import docker
import flexmock
import json
import pytest
import atomic_reactor

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.util import ImageName, CommandResult
from atomic_reactor.core import DockerTasker
from atomic_reactor.plugins.pre_pull_base_image import PullBaseImagePlugin
from tests.constants import MOCK, MOCK_SOURCE, LOCALHOST_REGISTRY

if MOCK:
    from tests.docker_mock import mock_docker


BASE_IMAGE = "busybox:latest"
BASE_IMAGE_W_LIBRARY = "library/" + BASE_IMAGE
BASE_IMAGE_W_REGISTRY = LOCALHOST_REGISTRY + "/" + BASE_IMAGE
BASE_IMAGE_W_LIB_REG = LOCALHOST_REGISTRY + "/" + BASE_IMAGE_W_LIBRARY
UNIQUE_ID = 'build-name-123'


class MockSource(object):
    dockerfile_path = None
    path = None


class MockBuilder(object):
    image_id = "xxx"
    source = MockSource()
    base_image = None

    def set_base_image(self, base_image):
        self.base_image = base_image
        assert base_image == UNIQUE_ID


@pytest.fixture(autouse=True)
def set_build_json(monkeypatch):
    monkeypatch.setenv("BUILD", json.dumps({
        'metadata': {
            'name': UNIQUE_ID,
        },
    }))


@pytest.mark.parametrize(('parent_registry',
                          'df_base',      # the base image is always expected unless
                                          # it's explicitly listed in 'not_expected'
                          'expected',      # additional expected images
                          'not_expected',  # additional images not expected
                          ), [
    (LOCALHOST_REGISTRY, BASE_IMAGE,
     # expected:
     [BASE_IMAGE_W_REGISTRY],
     # not expected:
     [BASE_IMAGE_W_LIB_REG]),

    (LOCALHOST_REGISTRY, BASE_IMAGE_W_REGISTRY,
     # expected:
     [BASE_IMAGE_W_REGISTRY],
     # not expected:
     [BASE_IMAGE_W_LIB_REG]),

    (None, BASE_IMAGE,
     # expected:
     [],
     # not expected:
     [BASE_IMAGE_W_REGISTRY, BASE_IMAGE_W_LIB_REG]),

    (None, BASE_IMAGE_W_REGISTRY,
     # expected:
     [BASE_IMAGE_W_REGISTRY],
     # not expected:
     [BASE_IMAGE_W_LIB_REG]),

    # Tests with explicit "library" namespace:

    (LOCALHOST_REGISTRY, BASE_IMAGE_W_LIB_REG,
     # expected:
     [],
     # not expected:
     [BASE_IMAGE_W_REGISTRY]),

    (None, BASE_IMAGE_W_LIB_REG,
     # expected:
     [],
     # not expected:
     [BASE_IMAGE_W_REGISTRY]),

    # For this test, ensure 'library-only' is only available through
    # the 'library' namespace. docker_mock takes care of this when
    # mocking.
    (LOCALHOST_REGISTRY, "library-only:latest",
     # expected:
     [LOCALHOST_REGISTRY + "/library/library-only:latest"],
     # not expected:
     ["library-only:latest",
      LOCALHOST_REGISTRY + "/library-only:latest"]),
])
def test_pull_base_image_plugin(parent_registry, df_base, expected, not_expected):
    if MOCK:
        mock_docker(remember_images=True)

    tasker = DockerTasker(retry_times=0)
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.builder = MockBuilder()
    workflow.builder.base_image = ImageName.parse(df_base)

    expected = set(expected)
    if df_base not in not_expected:
        expected.add(UNIQUE_ID)
    all_images = set(expected).union(not_expected)
    for image in all_images:
        assert not tasker.image_exists(image)

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': PullBaseImagePlugin.key,
            'args': {'parent_registry': parent_registry, 'parent_registry_insecure': True}
        }]
    )

    runner.run()

    for image in expected:
        assert tasker.image_exists(image)
        assert image in workflow.pulled_base_images

    for image in not_expected:
        assert not tasker.image_exists(image)

    for image in workflow.pulled_base_images:
        assert tasker.image_exists(image)

    for image in all_images:
        try:
            tasker.remove_image(image)
        except:
            pass


def test_pull_base_wrong_registry():
    with pytest.raises(PluginFailedException):
        test_pull_base_image_plugin('localhost:1234', BASE_IMAGE_W_REGISTRY, [], [])


def test_pull_base_base_parse():
    flexmock(ImageName).should_receive('parse').and_raise(AttributeError)
    with pytest.raises(AttributeError):
        test_pull_base_image_plugin(LOCALHOST_REGISTRY, BASE_IMAGE, [BASE_IMAGE_W_REGISTRY],
                                    [BASE_IMAGE_W_LIB_REG])


def test_pull_base_change_override(monkeypatch):
    monkeypatch.setenv("BUILD", json.dumps({
        'metadata': {
            'name': UNIQUE_ID,
        },
        'spec': {
            'triggeredBy': [
                {
                    'imageChangeBuild': {
                        'imageID': BASE_IMAGE
                    }
                },
            ]
        },
    }))
    test_pull_base_image_plugin(LOCALHOST_REGISTRY, 'invalid-image',
                                [BASE_IMAGE_W_REGISTRY], [BASE_IMAGE_W_LIB_REG])


@pytest.mark.parametrize(('exc', 'failures', 'should_succeed'), [
    (docker.errors.NotFound, 5, True),
    (docker.errors.NotFound, 25, False),
    (RuntimeError, 1, False),
])
def test_retry_pull_base_image(exc, failures, should_succeed):
    if MOCK:
        mock_docker(remember_images=True)

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.builder = MockBuilder()
    workflow.builder.base_image = ImageName.parse('parent-image')

    class MockResponse(object):
        content = ''

    expectation = flexmock(tasker).should_receive('tag_image')
    for _ in range(failures):
        expectation = expectation.and_raise(exc('', MockResponse()))

    expectation.and_return('foo')

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': PullBaseImagePlugin.key,
            'args': {'parent_registry': 'registry.example.com',
                     'parent_registry_insecure': True},
        }],
    )

    if should_succeed:
        runner.run()
    else:
        with pytest.raises(Exception):
            runner.run()


@pytest.mark.parametrize('library', [True, False])
def test_try_with_library_pull_base_image(library):
    if MOCK:
        mock_docker(remember_images=True)

    tasker = DockerTasker(retry_times=0)
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.builder = MockBuilder()

    if library:
        base_image = 'library/parent-image'
    else:
        base_image = 'parent-image'
    workflow.builder.base_image = ImageName.parse(base_image)

    class MockResponse(object):
        content = ''

    cr = CommandResult()
    cr._error = "cmd_error"
    cr._error_detail = {"message": "error_detail"}

    if library:
        call_wait = 1
    else:
        call_wait = 2

    (flexmock(atomic_reactor.util)
        .should_receive('wait_for_command')
        .times(call_wait)
        .and_return(cr))

    error_message = 'registry.example.com/' + base_image

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': PullBaseImagePlugin.key,
            'args': {'parent_registry': 'registry.example.com',
                     'parent_registry_insecure': True},
        }],
    )

    with pytest.raises(PluginFailedException) as exc:
        runner.run()

    assert error_message in exc.value.args[0]


def test_pull_scratch_base(caplog):
    test_pull_base_image_plugin('localhost:5000', 'scratch:latest', [], ['scratch:latest'])
    assert 'base image is scratch, do not attempt to pull' in caplog.text()
