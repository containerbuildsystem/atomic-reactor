"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import docker
import flexmock
import json
import sys
import pytest
import atomic_reactor
import atomic_reactor.util

from atomic_reactor.constants import (PLUGIN_BUILD_ORCHESTRATE_KEY,
                                      PLUGIN_CHECK_AND_SET_PLATFORMS_KEY,
                                      MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST,
                                      SCRATCH_FROM)
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.util import get_checksums, DockerfileImages
from atomic_reactor.core import DockerTasker
from atomic_reactor.plugins.pre_pull_base_image import PullBaseImagePlugin
from osbs.utils import ImageName
from io import BytesIO
from requests.exceptions import HTTPError, RetryError, Timeout
from tests.constants import (MOCK, LOCALHOST_REGISTRY,
                             IMAGE_RAISE_RETRYGENERATOREXCEPTION)


if MOCK:
    from tests.docker_mock import mock_docker


BASE_IMAGE = "busybox:latest"
BASE_IMAGE_W_LIBRARY = "library/" + BASE_IMAGE
BASE_IMAGE_W_REGISTRY = LOCALHOST_REGISTRY + "/" + BASE_IMAGE
BASE_IMAGE_W_LIB_REG = LOCALHOST_REGISTRY + "/" + BASE_IMAGE_W_LIBRARY
BASE_IMAGE_W_REGISTRY_SHA = LOCALHOST_REGISTRY + "/" +\
                            "busybox@sha256:19b0fc5d9581e28baf8d3e40a39bc"
BASE_IMAGE_W_SHA = "busybox@sha256:19b0fc5d9581e28baf8d3e40a39bc"
BASE_IMAGE_NAME = ImageName.parse(BASE_IMAGE)
BASE_IMAGE_NAME_W_LIBRARY = ImageName.parse(BASE_IMAGE_W_LIBRARY)
BASE_IMAGE_NAME_W_REGISTRY = ImageName.parse(BASE_IMAGE_W_REGISTRY)
BASE_IMAGE_NAME_W_LIB_REG = ImageName.parse(BASE_IMAGE_W_LIB_REG)
BASE_IMAGE_NAME_W_SHA = ImageName.parse(BASE_IMAGE_W_SHA)
UNIQUE_ID = 'build-name-123'
UNIQUE_ID_NAME = ImageName.parse(UNIQUE_ID)
SOURCE_REGISTRY = 'registry.example.com'


class MockSource(object):
    dockerfile_path = None
    path = None


class MockBuilder(object):
    image_id = "xxx"
    source = MockSource()

    def __init__(self):
        self.parent_images_digests = {}


@pytest.fixture(autouse=True)
def set_build_json(monkeypatch):
    monkeypatch.setenv("BUILD", json.dumps({
        'metadata': {
            'name': UNIQUE_ID,
        },
    }))


def teardown_function(function):
    sys.modules.pop('pre_pull_base_image', None)


@pytest.mark.parametrize('add_another_parent', [True, False])
@pytest.mark.parametrize(('special_image', 'change_base'), [
    ('koji/image-build', False),
    (SCRATCH_FROM, False),
    (BASE_IMAGE_W_REGISTRY, True)
])
def test_pull_base_image_special(add_another_parent, special_image, change_base, monkeypatch):
    monkeypatch.setenv("BUILD", json.dumps({
        'metadata': {
            'name': UNIQUE_ID,
        },
    }))
    monkeypatch.setenv('USER_PARAMS', json.dumps({'image_tag': special_image}))

    if MOCK:
        mock_docker(remember_images=True)

    tasker = DockerTasker(retry_times=0)
    buildstep_plugin = [{
        'name': PLUGIN_BUILD_ORCHESTRATE_KEY,
        'args': {'platforms': ['x86_64']},
    }]
    workflow = DockerBuildWorkflow(
        source=None,
        buildstep_plugins=buildstep_plugin,
    )
    workflow.builder = MockBuilder()
    dockerfile_images = [special_image]
    if add_another_parent:
        dockerfile_images.insert(0, BASE_IMAGE_W_REGISTRY_SHA)
    workflow.dockerfile_images = DockerfileImages(dockerfile_images)

    expected = []
    if special_image == SCRATCH_FROM:
        if add_another_parent:
            expected.append("{}:{}".format(UNIQUE_ID, 0))
    elif special_image == 'koji/image-build':
        if add_another_parent:
            expected.append("{}:{}".format(UNIQUE_ID, 1))
    else:
        expected.append("{}:{}".format(UNIQUE_ID, 0))
        if add_another_parent:
            expected.append("{}:{}".format(UNIQUE_ID, 1))

    for image in expected:
        assert not tasker.image_exists(image)

    rcm = {'version': 1, 'source_registry': {'url': LOCALHOST_REGISTRY, 'insecure': True},
           'registries_organization': None}
    workflow.conf.conf = rcm

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': PullBaseImagePlugin.key,
            'args': {}
        }]
    )

    runner.run()
    dockerfile_images = workflow.dockerfile_images
    if change_base:
        assert dockerfile_images.base_image.to_str().startswith(UNIQUE_ID)
    else:
        if dockerfile_images.base_from_scratch:
            assert dockerfile_images.base_image == special_image
        else:
            assert dockerfile_images.base_image.to_str().startswith(special_image)
    for image in expected:
        assert tasker.image_exists(image)
        assert image in workflow.pulled_base_images

    for image in workflow.pulled_base_images:
        assert tasker.image_exists(image)

    for df, tagged in dockerfile_images.items():
        if df.to_str().startswith('koji/image-build'):
            continue
        assert tagged is not None, "Did not tag parent image " + str(df)
    assert len(set(dockerfile_images.values())) == len(dockerfile_images)


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
])
def test_pull_base_image_plugin(user_params, parent_registry, df_base, expected, not_expected,
                                inspect_only, workflow_callback=None,
                                check_platforms=False, parent_images=None, organization=None,
                                parent_images_digests=None, expected_digests=None,
                                pull_registries=None):
    if MOCK:
        mock_docker(remember_images=True)

    tasker = DockerTasker(retry_times=0)
    buildstep_plugin = [{
        'name': PLUGIN_BUILD_ORCHESTRATE_KEY,
        'args': {'platforms': ['x86_64']},
    }]
    workflow = DockerBuildWorkflow(
        source=None,
        buildstep_plugins=buildstep_plugin,
    )
    workflow.builder = MockBuilder()

    if parent_images:
        dockerfile_images = parent_images
    else:
        add_base = ImageName.parse(df_base)
        if add_base.registry is None:
            add_base.registry = parent_registry
        dockerfile_images = [add_base.to_str()]
    workflow.dockerfile_images = DockerfileImages(dockerfile_images)

    expected = set(expected)
    if parent_images:
        for nonce in range(len(parent_images)):
            expected.add("{}:{}".format(UNIQUE_ID, nonce))
    else:
        expected.add("{}:{}".format(UNIQUE_ID, 0))

    all_images = set(expected).union(not_expected)
    for image in all_images:
        assert not tasker.image_exists(image)

    reactor_config = {'version': 1,
                      'source_registry': {'url': parent_registry,
                                          'insecure': True},
                      'registries_organization': organization}
    if pull_registries:
        reactor_config['pull_registries'] = pull_registries

    workflow.conf.conf = reactor_config

    if workflow_callback:
        workflow = workflow_callback(workflow)

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': PullBaseImagePlugin.key,
            'args': {'check_platforms': check_platforms,
                     'inspect_only': inspect_only,
                     'parent_images_digests': parent_images_digests,
                     }
        }]
    )

    if parent_registry is None:
        with pytest.raises(PluginFailedException):
            runner.run()
        return

    runner.run()
    if not inspect_only and not workflow.dockerfile_images.base_from_scratch:
        assert workflow.dockerfile_images.base_image.to_str().startswith(UNIQUE_ID + ":")

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

    dockerfile_images = workflow.dockerfile_images
    for df, tagged in dockerfile_images.items():
        assert tagged is not None, "Did not tag parent image " + str(df)
    # tags should all be unique
    assert len(set(dockerfile_images.values())) == len(dockerfile_images)
    if check_platforms and expected_digests:
        assert expected_digests == workflow.builder.parent_images_digests
    return workflow


@pytest.mark.parametrize('builder_registry', [  # noqa
    None,
    'pull_registry1.example.com',
    'pull_registry2.example.com'])
@pytest.mark.parametrize('organization', [None, 'my_organization'])
def test_pull_parent_images(builder_registry, organization, inspect_only, user_params):
    builder_image = 'builder:image'
    source_registry = 'registy_example.com'
    pull_url_1 = 'pull_registry1.example.com'
    pull_url_2 = 'pull_registry2.example.com'
    pull_insecure_1 = True
    pull_insecure_2 = False
    pull_registries = [{'url': pull_url_1,
                        'insecure': pull_insecure_1},
                       {'url': pull_url_2,
                        'insecure': pull_insecure_2}]
    exp_pull_reg = {source_registry: {'insecure': True, 'dockercfg_path': None}}

    if builder_registry:
        builder_image = "{}/{}".format(builder_registry, builder_image)
    else:
        builder_image = "{}/{}".format(source_registry, builder_image)
    base_image_name = "{}/{}".format(source_registry, BASE_IMAGE_NAME.copy().to_str())

    if builder_registry:
        if builder_registry == pull_url_1:
            exp_pull_reg[builder_registry] = {'insecure': pull_insecure_1, 'dockercfg_path': None}
        if builder_registry == pull_url_2:
            exp_pull_reg[builder_registry] = {'insecure': pull_insecure_2, 'dockercfg_path': None}
    parent_images = [builder_image, base_image_name]

    manifest_list = {
        'manifests': [
            {'platform': {'architecture': 'amd64'}, 'digest': 'sha256:123456'},
            {'platform': {'architecture': 'ppc64le'}, 'digest': 'sha256:654321'},
        ]
    }
    (flexmock(atomic_reactor.util.RegistryClient)
     .should_receive('get_manifest_list')
     .and_return(flexmock(json=lambda: manifest_list,
                          content=json.dumps(manifest_list).encode('utf-8'))))

    workflow = test_pull_base_image_plugin(
        user_params,
        source_registry, base_image_name,
        [   # expected to pull
            base_image_name,
            builder_image,
        ],
        [],  # should not be pulled
        inspect_only=inspect_only,
        check_platforms=inspect_only,
        parent_images=parent_images,
        organization=organization,
        pull_registries=pull_registries)

    assert workflow.builder.pull_registries == exp_pull_reg


def test_pull_base_wrong_registry(inspect_only, user_params):  # noqa
    source_registry = 'different.registry:5000'
    base_image_str = 'some.registry:8888/base:image'
    with pytest.raises(PluginFailedException) as exc:
        test_pull_base_image_plugin(
            user_params,
            source_registry, base_image_str, [], [],
            inspect_only=inspect_only
        )

    log_msg1 = "Registry specified in dockerfile image doesn't match allowed registries."
    assert log_msg1 in str(exc.value)
    assert "Dockerfile: '{}'".format(base_image_str) in str(exc.value)
    log_msg2 = "allowed registries: '%s'" % [source_registry]
    assert log_msg2 in str(exc.value)


def test_pull_parent_wrong_registry(inspect_only, user_params):  # noqa: F811
    source_registry = 'different.registry:5000'
    base_image_str = source_registry + "/base:image"
    parent_images = ["some.registry:8888/builder:image", base_image_str]
    with pytest.raises(PluginFailedException) as exc:
        test_pull_base_image_plugin(
            user_params,
            source_registry, base_image_str, [], [],
            inspect_only=inspect_only,
            parent_images=parent_images
        )

    log_msg1 = "Registry specified in dockerfile image doesn't match allowed registries."
    assert log_msg1 in str(exc.value)
    assert "Dockerfile: 'some.registry:8888/builder:image'" in str(exc.value)
    assert base_image_str not in str(exc.value)
    log_msg2 = "allowed registries: '%s'" % [source_registry]
    assert log_msg2 in str(exc.value)


def test_image_without_registry(inspect_only, user_params):  # noqa
    source_registry = 'source.registry:5000'
    base_image_str = 'builder:image'
    parent_images = [base_image_str]
    with pytest.raises(PluginFailedException) as exc:
        test_pull_base_image_plugin(
            user_params,
            source_registry, base_image_str, [], [],
            inspect_only=inspect_only,
            parent_images=parent_images
        )

    exc_msg = "raised an exception: RuntimeError: Shouldn't happen, images should have already " \
              "registry set in dockerfile_images"

    assert exc_msg in str(exc.value)


def test_pull_base_base_parse(inspect_only, user_params):  # noqa
    flexmock(ImageName).should_receive('parse').and_raise(AttributeError)
    with pytest.raises(AttributeError):
        test_pull_base_image_plugin(
            user_params,
            LOCALHOST_REGISTRY, BASE_IMAGE, [BASE_IMAGE_W_REGISTRY],
            [BASE_IMAGE_W_LIB_REG],
            inspect_only=inspect_only)


def test_pull_base_change_override(monkeypatch, inspect_only, user_params):  # noqa
    monkeypatch.setenv("BUILD", json.dumps({
        'metadata': {
            'name': UNIQUE_ID,
        },
        'spec': {
            'triggeredBy': [
                {
                    'imageChangeBuild': {
                        'imageID': '/'.join([LOCALHOST_REGISTRY, BASE_IMAGE])
                    }
                },
            ]
        },
    }))
    test_pull_base_image_plugin(
        user_params,
        LOCALHOST_REGISTRY, 'invalid-image',
        [BASE_IMAGE_W_REGISTRY], [BASE_IMAGE_W_LIB_REG],
        inspect_only=inspect_only)


def test_pull_base_autorebuild(monkeypatch, inspect_only, user_params):  # noqa
    mock_manifest_list = json.dumps({}).encode('utf-8')
    new_base_image = ImageName.parse(BASE_IMAGE)
    new_base_image.tag = 'newtag'
    new_base_image.registry = LOCALHOST_REGISTRY
    dgst = 'sha256:{}'.format(get_checksums(BytesIO(mock_manifest_list), ['sha256'])['sha256sum'])
    expected_digests = {BASE_IMAGE_W_REGISTRY: {MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST: dgst}}

    monkeypatch.setenv("BUILD", json.dumps({
        'metadata': {
            'name': UNIQUE_ID,
        },
        'spec': {
            'triggeredBy': [
                {
                    'imageChangeBuild': {
                        'imageID': new_base_image.to_str()
                    }
                },
            ]
        },
    }))

    (flexmock(atomic_reactor.util.RegistryClient)
     .should_receive('get_manifest_list')
     .and_return(flexmock(content=mock_manifest_list)))

    test_pull_base_image_plugin(user_params, LOCALHOST_REGISTRY, BASE_IMAGE,
                                [new_base_image.to_str()], [BASE_IMAGE_W_REGISTRY],
                                inspect_only=inspect_only, check_platforms=True,
                                expected_digests=expected_digests)


@pytest.mark.parametrize(('exc', 'failures', 'should_succeed'), [
    (docker.errors.NotFound, 5, True),
    (docker.errors.NotFound, 25, False),
    (RuntimeError, 1, False),
])
def test_retry_pull_base_image(workflow, exc, failures, should_succeed):
    if MOCK:
        mock_docker(remember_images=True)

    tasker = DockerTasker()
    workflow.builder = MockBuilder()
    source_registry = 'registry.example.com'
    base_image = '/'.join([source_registry, 'parent-image'])
    workflow.dockerfile_images = DockerfileImages([base_image])

    class MockResponse(object):
        content = ''

    expectation = flexmock(tasker).should_receive('tag_image')
    for _ in range(failures):
        expectation = expectation.and_raise(exc('', MockResponse()))

    expectation.and_return('foo')

    workflow.conf.conf = {'version': 1, 'source_registry': {'url': source_registry,
                                                            'insecure': True}}

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': PullBaseImagePlugin.key,
            'args': {},
        }],
    )

    if should_succeed:
        runner.run()
    else:
        with pytest.raises(Exception):
            runner.run()


def test_pull_raises_retry_error(workflow, caplog):
    if MOCK:
        mock_docker(remember_images=True)

    tasker = DockerTasker(retry_times=1)
    workflow.builder = MockBuilder()
    image_name = ImageName.parse(IMAGE_RAISE_RETRYGENERATOREXCEPTION)
    base_image_str = "{}/{}:{}".format(SOURCE_REGISTRY, image_name.repo, 'some')
    source_registry = image_name.registry
    workflow.dockerfile_images = DockerfileImages([base_image_str])
    workflow.conf.conf = {'version': 1, 'source_registry': {'url': source_registry,
                                                            'insecure': True}}

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': PullBaseImagePlugin.key,
            'args': {},
        }],
    )

    with pytest.raises(Exception):
        runner.run()

    exp_img = ImageName.parse(base_image_str)
    exp_img.registry = source_registry
    assert 'failed to pull image: {}'.format(exp_img.to_str()) in caplog.text


class TestValidateBaseImage(object):

    def teardown_method(self, method):
        sys.modules.pop('pre_pull_base_image', None)

    def test_manifest_list_verified(self, caplog, user_params):

        def workflow_callback(workflow):
            self.prepare(workflow, mock_get_manifest_list=True)
            return workflow

        log_message = 'manifest list for all required platforms'
        test_pull_base_image_plugin(user_params, SOURCE_REGISTRY, BASE_IMAGE,
                                    [], [], inspect_only=False,
                                    workflow_callback=workflow_callback,
                                    check_platforms=True)
        assert log_message in caplog.text

    def test_expected_platforms_unknown(self, caplog, user_params):

        def workflow_callback(workflow):
            self.prepare(workflow, mock_get_manifest_list=True)
            del workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY]
            del workflow.buildstep_plugins_conf[0]
            return workflow

        log_message = 'expected platforms are unknown'
        test_pull_base_image_plugin(user_params, SOURCE_REGISTRY, BASE_IMAGE,
                                    [], [], inspect_only=False,
                                    workflow_callback=workflow_callback,
                                    check_platforms=True)
        assert log_message in caplog.text

    @pytest.mark.parametrize('has_manifest_list', (True, False))
    @pytest.mark.parametrize('has_v2s2_manifest', (True, False))
    def test_single_platform_build(self, caplog, user_params, has_manifest_list, has_v2s2_manifest):

        class StubResponse(object):
            content = b'stubContent'

        def workflow_callback(workflow):
            if has_manifest_list:
                workflow = self.prepare(workflow, mock_get_manifest_list=True)
            else:
                workflow = self.prepare(workflow, mock_get_manifest_list=False)
            workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = {'x86_64'}
            if not has_manifest_list:
                resp = {'v2': StubResponse()} if has_v2s2_manifest else {}
                (flexmock(atomic_reactor.util.RegistryClient)
                 .should_receive('get_manifest_list')
                 .and_return(None))
                (flexmock(atomic_reactor.util.RegistryClient)
                 .should_receive('get_all_manifests')
                 .and_return(resp))
                if has_v2s2_manifest:
                    dgst = {'sha256sum': 'stubDigest'}
                    (flexmock(atomic_reactor.util)
                     .should_receive('get_checksums')
                     .and_return(dgst))

            return workflow

        if has_manifest_list or has_v2s2_manifest:
            list_msg = 'Base image is a manifest list for all required platforms'
            v2s2_msg = 'base image has no manifest list'
            log_message = list_msg if has_manifest_list else v2s2_msg
            test_pull_base_image_plugin(user_params, SOURCE_REGISTRY, BASE_IMAGE,
                                        [], [], inspect_only=False,
                                        workflow_callback=workflow_callback,
                                        check_platforms=True)
            assert log_message in caplog.text
        else:
            no_manifest_msg = 'Unable to fetch manifest list or v2 schema 2 digest'
            with pytest.raises(PluginFailedException) as exc:
                test_pull_base_image_plugin(user_params, SOURCE_REGISTRY, BASE_IMAGE,
                                            [], [], inspect_only=False,
                                            workflow_callback=workflow_callback,
                                            check_platforms=True)
            assert no_manifest_msg in str(exc.value)

    def test_manifest_list_with_no_response(self, user_params):
        def workflow_callback(workflow):
            workflow = self.prepare(workflow, mock_get_manifest_list=False)
            (flexmock(atomic_reactor.util.RegistryClient)
             .should_receive('get_manifest_list')
             .and_return(None))
            return workflow

        with pytest.raises(PluginFailedException) as exc_info:
            test_pull_base_image_plugin(user_params, SOURCE_REGISTRY, BASE_IMAGE,
                                        [], [],
                                        inspect_only=False,
                                        workflow_callback=workflow_callback,
                                        check_platforms=True)
        assert 'Unable to fetch manifest list' in str(exc_info.value)

    @pytest.mark.parametrize('existing_arches, missing_arches_str', [
        # Expected arches are amd64, ppc64le
        ([], 'amd64, ppc64le'),
        (['amd64'], 'ppc64le'),
        (['ppc64le'], 'amd64'),
    ])
    def test_manifest_list_missing_arches(self, existing_arches, missing_arches_str, user_params):
        def workflow_callback(workflow):
            workflow = self.prepare(workflow, mock_get_manifest_list=False)
            manifest_list = {
                'manifests': [
                    {'platform': {'architecture': arch}, 'digest': 'sha256:123456'}
                    for arch in existing_arches
                ]
            }
            (flexmock(atomic_reactor.util.RegistryClient)
             .should_receive('get_manifest_list')
             .and_return(flexmock(json=lambda: manifest_list)))
            return workflow

        with pytest.raises(PluginFailedException) as exc_info:
            test_pull_base_image_plugin(user_params, SOURCE_REGISTRY, BASE_IMAGE,
                                        [], [], inspect_only=False,
                                        workflow_callback=workflow_callback,
                                        check_platforms=True)

        base_image_with_registry = 'registry.example.com/{}'.format(BASE_IMAGE)
        expected_msg = ('Base image {} not available for arches: {}'
                        .format(base_image_with_registry, missing_arches_str))
        assert expected_msg in str(exc_info.value)

    @pytest.mark.parametrize('exception', (
        HTTPError,
        RetryError,
        Timeout,
    ))
    def test_manifest_config_raises(self, exception, user_params):
        class MockResponse(object):
            content = ''
            status_code = 408

        def workflow_callback(workflow):
            workflow = self.prepare(workflow, mock_get_manifest_list=False)
            (flexmock(atomic_reactor.util.RegistryClient)
             .should_receive('get_config_from_registry')
             .and_raise(exception('', response=MockResponse()))
             .once())

            manifest_tag = SOURCE_REGISTRY + '/' + BASE_IMAGE_W_SHA
            base_image_result = ImageName.parse(manifest_tag)
            manifest_image = base_image_result.copy()
            (flexmock(atomic_reactor.util.RegistryClient)
             .should_receive('get_manifest_list')
             .with_args(manifest_image)
             .and_return(None)
             .once())
            return workflow

        with pytest.raises(PluginFailedException) as exc_info:
            test_pull_base_image_plugin(user_params, SOURCE_REGISTRY, BASE_IMAGE_W_SHA,
                                        [], [], inspect_only=False,
                                        workflow_callback=workflow_callback,
                                        check_platforms=True)
        assert 'Unable to fetch config for base image' in str(exc_info.value)

    @pytest.mark.parametrize('sha_is_manifest_list', (
        True,
        False,
    ))
    def test_manifest_config_passes(self, sha_is_manifest_list, user_params):
        def workflow_callback(workflow):
            workflow = self.prepare(workflow, mock_get_manifest_list=False)
            release = 'rel1'
            version = 'ver1'
            config_blob = {'config': {'Labels': {'release': release, 'version': version}}}
            (flexmock(atomic_reactor.util.RegistryClient)
             .should_receive('get_config_from_registry')
             .and_return(config_blob)
             .times(0 if sha_is_manifest_list else 2))

            manifest_list = {
                'manifests': [
                    {'platform': {'architecture': 'amd64'}, 'digest': 'sha256:123456'},
                    {'platform': {'architecture': 'ppc64le'}, 'digest': 'sha256:654321'},
                ]
            }

            manifest_tag = SOURCE_REGISTRY + '/' + BASE_IMAGE_W_SHA
            base_image_result = ImageName.parse(manifest_tag)
            manifest_image_original = base_image_result.copy()

            if sha_is_manifest_list:
                (flexmock(atomic_reactor.util.RegistryClient)
                 .should_receive('get_manifest_list')
                 .with_args(manifest_image_original)
                 .and_return(flexmock(json=lambda: manifest_list,
                                      content=json.dumps(manifest_list).encode('utf-8')))
                 .once())
            else:
                (flexmock(atomic_reactor.util.RegistryClient)
                 .should_receive('get_manifest_list')
                 .with_args(manifest_image_original)
                 .and_return(None)
                 .times(2))
                docker_tag = '{}-{}'.format(version, release)
                manifest_tag = '{}/{}:{}'.\
                    format(SOURCE_REGISTRY,
                           BASE_IMAGE_W_SHA[:BASE_IMAGE_W_SHA.find('@sha256')],
                           docker_tag)
                base_image_result = ImageName.parse(manifest_tag)
                manifest_image_new = base_image_result.copy()
                (flexmock(atomic_reactor.util.RegistryClient)
                 .should_receive('get_manifest_list')
                 .with_args(manifest_image_new)
                 .and_return(flexmock(json=lambda: manifest_list,
                                      content=json.dumps(manifest_list).encode('utf-8')))
                 .times(2))
            return workflow

        test_pull_base_image_plugin(user_params, SOURCE_REGISTRY, BASE_IMAGE_W_SHA,
                                    [], [], inspect_only=False,
                                    workflow_callback=workflow_callback,
                                    check_platforms=True)

    def test_manifest_list_doesnt_have_current_platform(self, caplog, user_params):
        manifest_list = {
            'manifests': [
                {'platform': {'architecture': 'ppc64le'}, 'digest': 'sha256:654321'},
            ]
        }
        manifest_list_digest = get_checksums(BytesIO(json.dumps(manifest_list).encode('utf-8')),
                                             ['sha256'])['sha256sum']

        def workflow_callback(workflow):
            workflow = self.prepare(workflow, mock_get_manifest_list=False)
            workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = {'ppc64le'}
            release = 'rel1'
            version = 'ver1'
            config_blob = {'config': {'Labels': {'release': release, 'version': version}}}
            (flexmock(atomic_reactor.util.RegistryClient)
             .should_receive('get_config_from_registry')
             .and_return(config_blob)
             .times(0))

            manifest_tag = SOURCE_REGISTRY + '/' + BASE_IMAGE_W_SHA
            base_image_result = ImageName.parse(manifest_tag)
            manifest_image = base_image_result.copy()

            (flexmock(atomic_reactor.util.RegistryClient)
             .should_receive('get_manifest_list')
             .with_args(manifest_image)
             .and_return(flexmock(json=lambda: manifest_list,
                                  content=json.dumps(manifest_list).encode('utf-8')))
             .once())
            return workflow

        test_pull_base_image_plugin(user_params, SOURCE_REGISTRY, BASE_IMAGE_W_SHA,
                                    [], [], inspect_only=False,
                                    workflow_callback=workflow_callback,
                                    check_platforms=True)
        new_image = "'{}/busybox@sha256:{}'".format(SOURCE_REGISTRY, manifest_list_digest)
        pulling_msg = "pulling image " + new_image + " from registry"
        tagging_msg = "tagging image " + new_image + " as '" + UNIQUE_ID
        assert pulling_msg in caplog.text
        assert tagging_msg in caplog.text

    @pytest.mark.parametrize('fail', (True, False))
    def test_parent_images_digests_orchestrator(self, caplog, user_params, fail):
        """Testing processing of parent_images_digests at an orchestrator"""

        reg_image_no_tag = '{}/{}'.format(SOURCE_REGISTRY, BASE_IMAGE_NAME.to_str(tag=False))

        test_vals = {
            'workflow': None,
            'expected_digest': {}
        }
        if not fail:
            manifest_list = {
                'manifests': [
                    {'platform': {'architecture': 'amd64'}, 'digest': 'sha256:123456'},
                    {'platform': {'architecture': 'ppc64le'}, 'digest': 'sha256:654321'},
                ]
            }
            manifest_list_digest = get_checksums(BytesIO(json.dumps(manifest_list).encode('utf-8')),
                                                 ['sha256'])['sha256sum']
            digest = 'sha256:{}'.format(manifest_list_digest)
            test_vals['expected_digest'] = {
                '{}/{}'.format(SOURCE_REGISTRY, BASE_IMAGE): {
                    MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST: digest
                }
            }

        def workflow_callback(workflow):
            workflow = self.prepare(workflow, mock_get_manifest_list=not fail)
            if fail:
                # fail to provide x86_64 platform specific digest
                manifest_list = {
                    'manifests': []
                }

                (flexmock(atomic_reactor.util.RegistryClient)
                 .should_receive('get_manifest_list')
                 .and_return(flexmock(json=lambda: manifest_list,
                                      content=json.dumps(manifest_list).encode('utf-8')))
                 )

                # platform validation will fail if manifest is missing
                # setting only one platform to skip platform validation and test negative case
                workflow.buildstep_plugins_conf[0]['args']['platforms'] = ['x86_64']
                workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = {'x86_64'}

            test_vals['workflow'] = workflow
            return workflow

        if fail:
            with pytest.raises(PluginFailedException) as exc:
                test_pull_base_image_plugin(user_params, SOURCE_REGISTRY, BASE_IMAGE,
                                            [], [], inspect_only=False,
                                            workflow_callback=workflow_callback,
                                            check_platforms=True,  # orchestrator
                                            )
            assert 'not available for arches' in str(exc.value)
        else:
            test_pull_base_image_plugin(user_params, SOURCE_REGISTRY, BASE_IMAGE,
                                        [], [], inspect_only=False,
                                        workflow_callback=workflow_callback,
                                        check_platforms=True,  # orchestrator
                                        )

            replacing_msg = ("Replacing image '{}/{}' with '{}@sha256:{}'"
                             .format(SOURCE_REGISTRY, BASE_IMAGE, reg_image_no_tag,
                                     manifest_list_digest))
            assert replacing_msg in caplog.text

            # check if worker.builder has set correct values
            builder_digests_dict = test_vals['workflow'].builder.parent_images_digests
            assert builder_digests_dict == test_vals['expected_digest']

    @pytest.mark.parametrize('fail', ('no_expected_type', 'no_digests', False))
    def test_parent_images_digests_worker(self, caplog, user_params, fail):
        """Testing processing of parent_images_digests at a worker"""
        reg_image_no_tag = '{}/{}'.format(SOURCE_REGISTRY, BASE_IMAGE_NAME.to_str(tag=False))

        def workflow_callback(workflow):
            workflow = self.prepare(workflow, mock_get_manifest_list=False)
            return workflow

        if fail == 'no_expected_type':
            parent_images_digests = {
                '{}/{}'.format(SOURCE_REGISTRY, BASE_IMAGE): {
                    'unexpected_type': 'sha256:123456'
                }
            }
        elif fail == 'no_digests':
            parent_images_digests = None
        else:
            parent_images_digests = {
                '{}/{}'.format(SOURCE_REGISTRY, BASE_IMAGE): {
                    MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST: 'sha256:123456'
                }
            }

        test_pull_base_image_plugin(user_params, SOURCE_REGISTRY, BASE_IMAGE,
                                    [], [], inspect_only=False,
                                    workflow_callback=workflow_callback,
                                    check_platforms=False,  # worker
                                    parent_images_digests=parent_images_digests)

        if fail:
            replacing_msg = (
                "Cannot resolve manifest digest for image "
                "'registry.example.com/{}'".format(BASE_IMAGE))
        else:
            replacing_msg = ("Replacing image 'registry.example.com/{}' with "
                             "'{}@sha256:123456'".format(
                                BASE_IMAGE, reg_image_no_tag))
        assert replacing_msg in caplog.text

    def prepare(self, workflow, mock_get_manifest_list=False):
        # Setup expected platforms
        workflow.buildstep_plugins_conf[0]['args']['platforms'] = ['x86_64', 'ppc64le']
        workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = {'x86_64', 'ppc64le'}

        # Setup platform descriptors
        workflow.conf.conf = {'version': 1,
                              'source_registry': {'url': SOURCE_REGISTRY, 'insecure': True},
                              'platform_descriptors': [{'platform': 'x86_64',
                                                        'architecture': 'amd64'}]}

        if mock_get_manifest_list:
            # Setup multi-arch manifest list
            manifest_list = {
                'manifests': [
                    {'platform': {'architecture': 'amd64'}, 'digest': 'sha256:123456'},
                    {'platform': {'architecture': 'ppc64le'}, 'digest': 'sha256:654321'},
                ]
            }
            (flexmock(atomic_reactor.util.RegistryClient)
             .should_receive('get_manifest_list')
             .and_return(flexmock(json=lambda: manifest_list,
                                  content=json.dumps(manifest_list).encode('utf-8'))))

        return workflow
