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
import sys
import pytest
import atomic_reactor
import atomic_reactor.util

from atomic_reactor.constants import (PLUGIN_BUILD_ORCHESTRATE_KEY,
                                      PLUGIN_CHECK_AND_SET_PLATFORMS_KEY)
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.util import ImageName, CommandResult
from atomic_reactor.core import DockerTasker
from atomic_reactor.plugins.pre_pull_base_image import PullBaseImagePlugin
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from requests.exceptions import HTTPError, RetryError, Timeout
from tests.constants import MOCK, MOCK_SOURCE, LOCALHOST_REGISTRY

if MOCK:
    from tests.docker_mock import mock_docker


BASE_IMAGE = "busybox:latest"
BASE_IMAGE_W_LIBRARY = "library/" + BASE_IMAGE
BASE_IMAGE_W_REGISTRY = LOCALHOST_REGISTRY + "/" + BASE_IMAGE
BASE_IMAGE_W_LIB_REG = LOCALHOST_REGISTRY + "/" + BASE_IMAGE_W_LIBRARY
BASE_IMAGE_W_SHA = "busybox@sha256:19b0fc5d9581e28baf8d3e40a39bc"
BASE_IMAGE_NAME = ImageName.parse(BASE_IMAGE)
BASE_IMAGE_NAME_W_LIBRARY = ImageName.parse(BASE_IMAGE_W_LIBRARY)
BASE_IMAGE_NAME_W_REGISTRY = ImageName.parse(BASE_IMAGE_W_REGISTRY)
BASE_IMAGE_NAME_W_LIB_REG = ImageName.parse(BASE_IMAGE_W_LIB_REG)
BASE_IMAGE_NAME_W_SHA = ImageName.parse(BASE_IMAGE_W_SHA)
UNIQUE_ID = 'build-name-123'
UNIQUE_ID_NAME = ImageName.parse(UNIQUE_ID)


class MockSource(object):
    dockerfile_path = None
    path = None


class MockBuilder(object):
    image_id = "xxx"
    source = MockSource()
    base_image = None
    original_base_image = None
    parent_images = {UNIQUE_ID_NAME: None}

    def set_base_image(self, base_image, parents_pulled=True, insecure=False):
        self.base_image = ImageName.parse(base_image)
        self.original_base_image = self.original_base_image or self.base_image

    def recreate_parent_images(self):
        # recreate parent_images to update hashes
        parent_images = {}
        for key, val in self.parent_images.items():
            parent_images[key] = val
        self.parent_images = parent_images


@pytest.fixture(autouse=True)
def set_build_json(monkeypatch):
    monkeypatch.setenv("BUILD", json.dumps({
        'metadata': {
            'name': UNIQUE_ID,
        },
    }))


def teardown_function(function):
    sys.modules.pop('pre_pull_base_image', None)


@pytest.mark.parametrize(('parent_registry',
                          'df_base',       # unique ID is always expected
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
def test_pull_base_image_plugin(parent_registry, df_base, expected, not_expected,
                                reactor_config_map, inspect_only, workflow_callback=None,
                                check_platforms=False, parent_images=None, organization=None):
    if MOCK:
        mock_docker(remember_images=True)

    tasker = DockerTasker(retry_times=0)
    buildstep_plugin = [{
        'name': PLUGIN_BUILD_ORCHESTRATE_KEY,
        'args': {'platforms': ['x86_64']},
    }]
    parent_images = parent_images or {ImageName.parse(df_base): None}
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image', buildstep_plugins=buildstep_plugin,)
    builder = workflow.builder = MockBuilder()
    builder.base_image = builder.original_base_image = ImageName.parse(df_base)
    builder.parent_images = parent_images

    expected = set(expected)
    for nonce in range(len(parent_images)):
        expected.add("{}:{}".format(UNIQUE_ID, nonce))
    all_images = set(expected).union(not_expected)
    for image in all_images:
        assert not tasker.image_exists(image)

    if reactor_config_map:
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig({'version': 1,
                           'source_registry': {'url': parent_registry,
                                               'insecure': True},
                           'registries_organization': organization})

    if workflow_callback:
        workflow = workflow_callback(workflow)

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': PullBaseImagePlugin.key,
            'args': {'parent_registry': parent_registry,
                     'parent_registry_insecure': True,
                     'check_platforms': check_platforms,
                     'inspect_only': inspect_only}
        }]
    )

    if parent_registry is None and reactor_config_map:
        with pytest.raises(PluginFailedException):
            runner.run()
        return

    runner.run()
    if not inspect_only:
        assert workflow.builder.base_image.to_str().startswith(UNIQUE_ID + ":")

    for image in expected:
        if inspect_only:
            assert not tasker.image_exists(image)
            assert image not in workflow.pulled_base_images
        else:
            assert tasker.image_exists(image)
            assert image in workflow.pulled_base_images

    for image in not_expected:
        assert not tasker.image_exists(image)

    for image in workflow.pulled_base_images:
        assert tasker.image_exists(image)

    for df, tagged in workflow.builder.parent_images.items():
        assert tagged is not None, "Did not tag parent image " + str(df)
    # tags should all be unique
    assert len(set(workflow.builder.parent_images.values())) == len(workflow.builder.parent_images)


@pytest.mark.parametrize('organization', [None, 'my_organization'])  # noqa
def test_pull_parent_images(organization, reactor_config_map, inspect_only):
    builder_image = 'builder:image'
    parent_images = {BASE_IMAGE_NAME.copy(): None, ImageName.parse(builder_image): None}

    enclosed_base_image = BASE_IMAGE_W_REGISTRY
    enclosed_builder_image = LOCALHOST_REGISTRY + '/' + builder_image
    if organization and reactor_config_map:
        base_image_name = ImageName.parse(enclosed_base_image)
        base_image_name.enclose(organization)
        enclosed_base_image = base_image_name.to_str()
        builder_image_name = ImageName.parse(enclosed_builder_image)
        builder_image_name.enclose(organization)
        enclosed_builder_image = builder_image_name.to_str()

    test_pull_base_image_plugin(
        LOCALHOST_REGISTRY, BASE_IMAGE,
        [   # expected to pull
            enclosed_base_image,
            enclosed_builder_image,
        ],
        [],  # should not be pulled
        reactor_config_map=reactor_config_map,
        inspect_only=inspect_only,
        parent_images=parent_images,
        organization=organization)


def test_pull_base_wrong_registry(reactor_config_map, inspect_only):  # noqa
    with pytest.raises(PluginFailedException) as exc:
        test_pull_base_image_plugin(
            'different.registry:5000', "some.registry:8888/base:image", [], [],
            reactor_config_map=reactor_config_map,
            inspect_only=inspect_only
        )
    assert "expected registry: 'different.registry:5000'" in str(exc.value)


def test_pull_parent_wrong_registry(reactor_config_map, inspect_only):  # noqa: F811
    parent_images = {
        ImageName.parse("base:image"): None,
        ImageName.parse("some.registry:8888/builder:image"): None}
    with pytest.raises(PluginFailedException) as exc:
        test_pull_base_image_plugin(
            'different.registry:5000', "base:image", [], [],
            reactor_config_map=reactor_config_map,
            inspect_only=inspect_only,
            parent_images=parent_images
        )
    assert "Dockerfile: 'some.registry:8888/builder:image'" in str(exc.value)
    assert "expected registry: 'different.registry:5000'" in str(exc.value)
    assert "base:image" not in str(exc.value)


# test previous issue https://github.com/projectatomic/atomic-reactor/issues/1008
def test_pull_base_library(reactor_config_map, caplog):  # noqa
    with pytest.raises(PluginFailedException) as exc:
        test_pull_base_image_plugin(
            LOCALHOST_REGISTRY, "spam/library-only:latest", [], [],
            reactor_config_map, False
        )
    assert "not found" in str(exc.value)
    assert "RetryGeneratorException" in str(exc.value)
    assert "trying" not in caplog.text()  # don't retry with "library/library-only:latest"


def test_pull_base_base_parse(reactor_config_map, inspect_only):  # noqa
    flexmock(ImageName).should_receive('parse').and_raise(AttributeError)
    with pytest.raises(AttributeError):
        test_pull_base_image_plugin(LOCALHOST_REGISTRY, BASE_IMAGE, [BASE_IMAGE_W_REGISTRY],
                                    [BASE_IMAGE_W_LIB_REG],
                                    reactor_config_map=reactor_config_map,
                                    inspect_only=inspect_only)


def test_pull_base_change_override(monkeypatch, reactor_config_map, inspect_only):  # noqa
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
                                [BASE_IMAGE_W_REGISTRY], [BASE_IMAGE_W_LIB_REG],
                                reactor_config_map=reactor_config_map,
                                inspect_only=inspect_only)


@pytest.mark.parametrize(('exc', 'failures', 'should_succeed'), [
    (docker.errors.NotFound, 5, True),
    (docker.errors.NotFound, 25, False),
    (RuntimeError, 1, False),
])
def test_retry_pull_base_image(exc, failures, should_succeed, reactor_config_map):
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

    if reactor_config_map:
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig({'version': 1,
                           'source_registry': {'url': 'registry.example.com',
                                               'insecure': True}})

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
def test_try_with_library_pull_base_image(library, reactor_config_map):
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
    workflow.builder.parent_images = {ImageName.parse(base_image): None}

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

    if reactor_config_map:
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig({'version': 1,
                           'source_registry': {'url': 'registry.example.com',
                                               'insecure': True}})

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


class TestValidateBaseImage(object):

    def teardown_method(self, method):
        sys.modules.pop('pre_pull_base_image', None)

    def test_manifest_list_verified(self, caplog):
        log_message = 'manifest list for all required platforms'
        test_pull_base_image_plugin(LOCALHOST_REGISTRY, BASE_IMAGE,
                                    [], [], reactor_config_map=True,
                                    inspect_only=False,
                                    workflow_callback=self.prepare,
                                    check_platforms=True)
        assert log_message in caplog.text()

    def test_expected_platforms_unknown(self, caplog):

        def workflow_callback(workflow):
            self.prepare(workflow)
            del workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY]
            del workflow.buildstep_plugins_conf[0]
            return workflow

        log_message = 'expected platforms are unknown'
        test_pull_base_image_plugin(LOCALHOST_REGISTRY, BASE_IMAGE,
                                    [], [], reactor_config_map=True,
                                    inspect_only=False,
                                    workflow_callback=workflow_callback,
                                    check_platforms=True)
        assert log_message in caplog.text()

    def test_single_platform_build(self, caplog):

        def workflow_callback(workflow):
            workflow = self.prepare(workflow)
            workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = set(['x86_64'])
            return workflow

        log_message = 'single platform build'
        test_pull_base_image_plugin(LOCALHOST_REGISTRY, BASE_IMAGE,
                                    [], [], reactor_config_map=True,
                                    inspect_only=False,
                                    workflow_callback=workflow_callback,
                                    check_platforms=True)
        assert log_message in caplog.text()

    def test_registry_undefined(self, caplog):
        def workflow_callback(workflow):
            workflow = self.prepare(workflow)
            reactor_config = workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY]
            del reactor_config.conf['source_registry']
            return workflow

        log_message = 'base image registry is not defined'
        test_pull_base_image_plugin('', BASE_IMAGE,
                                    [], [], reactor_config_map=True,
                                    inspect_only=False,
                                    workflow_callback=workflow_callback,
                                    check_platforms=True)
        assert log_message in caplog.text()

    def test_platform_descriptors_undefined(self, caplog):
        def workflow_callback(workflow):
            workflow = self.prepare(workflow)
            reactor_config = workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY]
            del reactor_config.conf['platform_descriptors']
            return workflow

        log_message = 'platform descriptors are not defined'
        test_pull_base_image_plugin(LOCALHOST_REGISTRY, BASE_IMAGE,
                                    [], [], reactor_config_map=True,
                                    inspect_only=False,
                                    workflow_callback=workflow_callback,
                                    check_platforms=True)
        assert log_message in caplog.text()

    def test_manifest_list_with_no_response(self, caplog):
        def workflow_callback(workflow):
            workflow = self.prepare(workflow)
            (flexmock(atomic_reactor.util)
             .should_receive('get_manifest_list')
             .and_return(None))
            return workflow

        with pytest.raises(PluginFailedException) as exc_info:
            test_pull_base_image_plugin(LOCALHOST_REGISTRY, BASE_IMAGE,
                                        [], [], reactor_config_map=True,
                                        inspect_only=False,
                                        workflow_callback=workflow_callback,
                                        check_platforms=True)
        assert 'Unable to fetch manifest list' in str(exc_info.value)

    def test_manifest_list_missing_arches(self):
        def workflow_callback(workflow):
            workflow = self.prepare(workflow)
            manifest_list = {
                'manifests': [
                    {'platform': {'architecture': 'amd64'}, 'digest': 'sha256:123456'},
                ]
            }
            (flexmock(atomic_reactor.util)
             .should_receive('get_manifest_list')
             .and_return(flexmock(json=lambda: manifest_list)))
            return workflow

        with pytest.raises(PluginFailedException) as exc_info:
            test_pull_base_image_plugin(LOCALHOST_REGISTRY, BASE_IMAGE,
                                        [], [], reactor_config_map=True,
                                        inspect_only=False,
                                        workflow_callback=workflow_callback,
                                        check_platforms=True)
        assert 'Missing arches in manifest list' in str(exc_info.value)

    @pytest.mark.parametrize('exception', (
        HTTPError,
        RetryError,
        Timeout,
    ))
    def test_manifest_config_raises(self, caplog, exception):
        class MockResponse(object):
            content = ''
            status_code = 408

        def workflow_callback(workflow):
            workflow = self.prepare(workflow)
            (flexmock(atomic_reactor.util)
             .should_receive('get_config_from_registry')
             .and_raise(exception('', response=MockResponse()))
             .once())

            manifest_tag = 'registry.example.com' + '/' + BASE_IMAGE_W_SHA
            base_image_result = ImageName.parse(manifest_tag)
            manifest_image = base_image_result.copy()
            (flexmock(atomic_reactor.util)
             .should_receive('get_manifest_list')
             .with_args(image=manifest_image, registry=manifest_image.registry, insecure=True)
             .and_return(None)
             .once())
            return workflow

        with pytest.raises(PluginFailedException) as exc_info:
            test_pull_base_image_plugin(LOCALHOST_REGISTRY, BASE_IMAGE_W_SHA,
                                        [], [], reactor_config_map=True,
                                        inspect_only=False,
                                        workflow_callback=workflow_callback,
                                        check_platforms=True)
        assert 'Unable to fetch config for base image' in str(exc_info.value)

    @pytest.mark.parametrize('sha_is_manifest_list', (
        True,
        False,
    ))
    def test_manifest_config_passes(self, sha_is_manifest_list):
        def workflow_callback(workflow):
            workflow = self.prepare(workflow)
            release = 'rel1'
            version = 'ver1'
            config_blob = {'config': {'Labels': {'release': release, 'version': version}}}
            (flexmock(atomic_reactor.util)
             .should_receive('get_config_from_registry')
             .and_return(config_blob)
             .times(0 if sha_is_manifest_list else 1))

            manifest_list = {
                'manifests': [
                    {'platform': {'architecture': 'amd64'}, 'digest': 'sha256:123456'},
                    {'platform': {'architecture': 'ppc64le'}, 'digest': 'sha256:654321'},
                ]
            }

            manifest_tag = 'registry.example.com' + '/' + BASE_IMAGE_W_SHA
            base_image_result = ImageName.parse(manifest_tag)
            manifest_image = base_image_result.copy()

            if sha_is_manifest_list:
                (flexmock(atomic_reactor.util)
                 .should_receive('get_manifest_list')
                 .with_args(image=manifest_image, registry=manifest_image.registry, insecure=True)
                 .and_return(flexmock(json=lambda: manifest_list))
                 .once())
            else:
                (flexmock(atomic_reactor.util)
                 .should_receive('get_manifest_list')
                 .with_args(image=manifest_image, registry=manifest_image.registry, insecure=True)
                 .and_return(None)
                 .once()
                 .ordered())

                docker_tag = "%s-%s" % (version, release)
                manifest_tag = 'registry.example.com' + '/' +\
                               BASE_IMAGE_W_SHA[:BASE_IMAGE_W_SHA.find('@sha256')] +\
                               ':' + docker_tag
                base_image_result = ImageName.parse(manifest_tag)
                manifest_image = base_image_result.copy()
                (flexmock(atomic_reactor.util)
                 .should_receive('get_manifest_list')
                 .with_args(image=manifest_image, registry=manifest_image.registry, insecure=True)
                 .and_return(flexmock(json=lambda: manifest_list))
                 .once()
                 .ordered())
            return workflow

        test_pull_base_image_plugin(LOCALHOST_REGISTRY, BASE_IMAGE_W_SHA,
                                    [], [], reactor_config_map=True,
                                    inspect_only=False,
                                    workflow_callback=workflow_callback,
                                    check_platforms=True)

    def test_manifest_list_doesnt_have_current_platform(self, caplog):
        def workflow_callback(workflow):
            workflow = self.prepare(workflow)
            workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = set(['ppc64le'])
            release = 'rel1'
            version = 'ver1'
            config_blob = {'config': {'Labels': {'release': release, 'version': version}}}
            (flexmock(atomic_reactor.util)
             .should_receive('get_config_from_registry')
             .and_return(config_blob)
             .times(0))

            manifest_list = {
                'manifests': [
                    {'platform': {'architecture': 'ppc64le'}, 'digest': 'sha256:654321'},
                ]
            }

            manifest_tag = 'registry.example.com' + '/' + BASE_IMAGE_W_SHA
            base_image_result = ImageName.parse(manifest_tag)
            manifest_image = base_image_result.copy()

            (flexmock(atomic_reactor.util)
             .should_receive('get_manifest_list')
             .with_args(image=manifest_image, registry=manifest_image.registry, insecure=True)
             .and_return(flexmock(json=lambda: manifest_list))
             .once())
            return workflow

        test_pull_base_image_plugin(LOCALHOST_REGISTRY, BASE_IMAGE_W_SHA,
                                    [], [], reactor_config_map=True,
                                    inspect_only=False,
                                    workflow_callback=workflow_callback,
                                    check_platforms=True)
        new_image = "'registry.example.com/busybox@sha256:654321'"
        pulling_msg = "pulling image " + new_image + " from registry"
        tagging_msg = "tagging image " + new_image + " as '" + UNIQUE_ID
        assert pulling_msg in caplog.text()
        assert tagging_msg in caplog.text()

    def prepare(self, workflow):
        # Setup expected platforms
        workflow.buildstep_plugins_conf[0]['args']['platforms'] = ['x86_64', 'ppc64le']
        workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = set(['x86_64', 'ppc64le'])

        # Setup platform descriptors
        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig({
                'version': 1,
                'source_registry': {'url': 'registry.example.com', 'insecure': True},
                'platform_descriptors': [{'platform': 'x86_64', 'architecture': 'amd64'}],
            })

        # Setup multi-arch manifest list
        manifest_list = {
            'manifests': [
                {'platform': {'architecture': 'amd64'}, 'digest': 'sha256:123456'},
                {'platform': {'architecture': 'ppc64le'}, 'digest': 'sha256:654321'},
            ]
        }
        (flexmock(atomic_reactor.util)
         .should_receive('get_manifest_list')
         .and_return(flexmock(json=lambda: manifest_list)))

        return workflow
