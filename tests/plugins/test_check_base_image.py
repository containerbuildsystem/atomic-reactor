"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import json
import sys
from io import BytesIO

import pytest
from flexmock import flexmock
from osbs.utils import ImageName
from requests.exceptions import HTTPError, RetryError, Timeout

import atomic_reactor
import atomic_reactor.util
from atomic_reactor.constants import (PLUGIN_CHECK_AND_SET_PLATFORMS_KEY,
                                      MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST,
                                      SCRATCH_FROM)
from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.pre_check_base_image import CheckBaseImagePlugin
from atomic_reactor.util import get_checksums
from tests.constants import (LOCALHOST_REGISTRY)
from tests.mock_env import MockEnv

BASE_IMAGE = "busybox:latest"
BASE_IMAGE_W_LIBRARY = "library/" + BASE_IMAGE
BASE_IMAGE_W_REGISTRY = LOCALHOST_REGISTRY + "/" + BASE_IMAGE
BASE_IMAGE_W_LIB_REG = LOCALHOST_REGISTRY + "/" + BASE_IMAGE_W_LIBRARY
BASE_IMAGE_W_REGISTRY_SHA = LOCALHOST_REGISTRY + "/" + \
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


def teardown_function(function):
    sys.modules.pop('pre_check_base_image', None)


def mock_env(workflow):
    return MockEnv(workflow).for_plugin("prebuild", CheckBaseImagePlugin.key)


@pytest.mark.parametrize('add_another_parent', [True, False])
@pytest.mark.parametrize('special_image', [
    'koji/image-build',
    SCRATCH_FROM,
])
def test_check_base_image_special(add_another_parent, special_image, workflow):
    env = (
        mock_env(workflow).set_user_params(image_tag=special_image)
        .set_orchestrator_platforms(["x86_64"])
    )

    dockerfile_images = [special_image]
    if add_another_parent:
        dockerfile_images.insert(0, BASE_IMAGE_W_REGISTRY_SHA)
    env.set_dockerfile_images(dockerfile_images)

    rcm = {'version': 1, 'source_registry': {'url': LOCALHOST_REGISTRY, 'insecure': True},
           'registries_organization': None}
    env.set_reactor_config(rcm)
    runner = env.create_runner()

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

    runner.run()
    dockerfile_images = env.workflow.dockerfile_images
    if dockerfile_images.base_from_scratch:
        assert dockerfile_images.base_image == special_image
    else:
        assert dockerfile_images.base_image.to_str().startswith(special_image)

    assert len(set(dockerfile_images.values())) == len(dockerfile_images)


@pytest.mark.parametrize(('parent_registry', 'df_base'), [
                             (LOCALHOST_REGISTRY, BASE_IMAGE),

                             (LOCALHOST_REGISTRY, BASE_IMAGE_W_REGISTRY),

                             (None, BASE_IMAGE),

                             (None, BASE_IMAGE_W_REGISTRY),

                             # Tests with explicit "library" namespace:

                             (LOCALHOST_REGISTRY, BASE_IMAGE_W_LIB_REG),

                             (None, BASE_IMAGE_W_LIB_REG),
                         ])
def test_check_base_image_plugin(workflow, parent_registry, df_base,
                                 workflow_callback=None, parent_images=None, organization=None,
                                 expected_digests=None, pull_registries=None,
                                 mock_get_manifest_list=True):
    env = mock_env(workflow).set_orchestrator_platforms(["x86_64"])
    add_base = None

    if parent_images:
        dockerfile_images = parent_images
    else:
        add_base = ImageName.parse(df_base)
        if add_base.registry is None:
            add_base.registry = parent_registry
        dockerfile_images = [add_base.to_str()]
    env.set_dockerfile_images(dockerfile_images)

    reactor_config = {'version': 1,
                      'source_registry': {'url': parent_registry,
                                          'insecure': True},
                      'registries_organization': organization}
    if pull_registries:
        reactor_config['pull_registries'] = pull_registries

    env.set_reactor_config(reactor_config)

    workflow = env.workflow
    if workflow_callback:
        workflow = workflow_callback(workflow)

    runner = env.create_runner()
    env.set_user_params(pipeline_run_name=UNIQUE_ID)

    if parent_registry is None:
        with pytest.raises(PluginFailedException):
            runner.run()
        return

    manifest_list = {
        'manifests': [
            {'platform': {'architecture': 'amd64'}, 'digest': 'sha256:123456'},
            {'platform': {'architecture': 'ppc64le'}, 'digest': 'sha256:654321'},
        ]
    }

    if mock_get_manifest_list:
        (flexmock(atomic_reactor.util.RegistryClient)
         .should_receive('get_manifest_list')
         .with_args(add_base)
         .and_return(flexmock(json=lambda: manifest_list,
                              content=json.dumps(manifest_list).encode('utf-8'))))

    runner.run()

    dockerfile_images = workflow.dockerfile_images
    for df, tagged in dockerfile_images.items():
        assert tagged is not None, "Did not tag parent image " + str(df)
        assert 'sha256' in tagged.to_str()
    # tags should all be unique
    assert len(set(dockerfile_images.values())) == len(dockerfile_images)
    if expected_digests:
        assert expected_digests == workflow.parent_images_digests
    return workflow


@pytest.mark.parametrize('builder_registry', [
    None,
    'pull_registry1.example.com',
    'pull_registry2.example.com'])
@pytest.mark.parametrize('organization', [None, 'my_organization'])
def test_check_parent_images(builder_registry, organization, workflow):
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

    test_check_base_image_plugin(
        workflow,
        source_registry, base_image_name,
        parent_images=parent_images,
        organization=organization,
        pull_registries=pull_registries)


def test_check_base_wrong_registry(workflow):
    source_registry = 'different.registry:5000'
    base_image_str = 'some.registry:8888/base:image'
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
    with pytest.raises(PluginFailedException) as exc:
        test_check_base_image_plugin(
            workflow,
            source_registry, base_image_str, [], []
        )

    log_msg1 = "Registry specified in dockerfile image doesn't match allowed registries."
    assert log_msg1 in str(exc.value)
    assert "Dockerfile: '{}'".format(base_image_str) in str(exc.value)
    log_msg2 = "allowed registries: '%s'" % [source_registry]
    assert log_msg2 in str(exc.value)


def test_check_parent_wrong_registry(workflow):  # noqa: F811
    source_registry = 'different.registry:5000'
    base_image_str = source_registry + "/base:image"
    parent_images = ["some.registry:8888/builder:image", base_image_str]
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
    with pytest.raises(PluginFailedException) as exc:
        test_check_base_image_plugin(
            workflow,
            source_registry, base_image_str,
            parent_images=parent_images
        )

    log_msg1 = "Registry specified in dockerfile image doesn't match allowed registries."
    assert log_msg1 in str(exc.value)
    assert "Dockerfile: 'some.registry:8888/builder:image'" in str(exc.value)
    assert base_image_str not in str(exc.value)
    log_msg2 = "allowed registries: '%s'" % [source_registry]
    assert log_msg2 in str(exc.value)


def test_image_without_registry(workflow):
    source_registry = 'source.registry:5000'
    base_image_str = 'builder:image'
    parent_images = [base_image_str]
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
    with pytest.raises(PluginFailedException) as exc:
        test_check_base_image_plugin(
            workflow,
            source_registry, base_image_str,
            parent_images=parent_images
        )

    exc_msg = "raised an exception: RuntimeError: Shouldn't happen, images should have already " \
              "registry set in dockerfile_images"

    assert exc_msg in str(exc.value)


def test_check_base_parse(workflow):
    flexmock(ImageName).should_receive('parse').and_raise(AttributeError)
    with pytest.raises(AttributeError):
        test_check_base_image_plugin(
            workflow,
            LOCALHOST_REGISTRY, BASE_IMAGE, [BASE_IMAGE_W_REGISTRY],
            [BASE_IMAGE_W_LIB_REG])


class TestValidateBaseImage(object):

    def teardown_method(self, method):
        sys.modules.pop('pre_check_base_image', None)

    def test_manifest_list_verified(self, workflow, caplog):

        def workflow_callback(workflow):
            self.prepare(workflow, mock_get_manifest_list=True)
            return workflow

        log_message = 'manifest list for all required platforms'
        test_check_base_image_plugin(workflow, SOURCE_REGISTRY, BASE_IMAGE,
                                     workflow_callback=workflow_callback)
        assert log_message in caplog.text

    def test_expected_platforms_unknown(self, caplog, workflow):

        def workflow_callback(workflow):
            self.prepare(workflow, mock_get_manifest_list=True)
            del workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY]
            del workflow.plugins.buildstep[0]
            return workflow

        log_message = 'expected platforms are unknown'
        test_check_base_image_plugin(workflow, SOURCE_REGISTRY, BASE_IMAGE,
                                     workflow_callback=workflow_callback)
        assert log_message in caplog.text

    @pytest.mark.parametrize('has_manifest_list', (True, False))
    @pytest.mark.parametrize('has_v2s2_manifest', (True, False))
    def test_single_platform_build(self, caplog, workflow, has_manifest_list, has_v2s2_manifest):

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
            test_check_base_image_plugin(workflow, SOURCE_REGISTRY, BASE_IMAGE,
                                         workflow_callback=workflow_callback,
                                         mock_get_manifest_list=False)
            assert log_message in caplog.text
        else:
            no_manifest_msg = 'Unable to fetch manifest list or v2 schema 2 digest'
            expected_digests = {'registry.example.com/busybox:latest': {
                'application/vnd.docker.distribution.manifest.list.v2+json':
                    'sha256:dd796ff42339f8a7c109da5c4c63148e9e0798ab029e323e54c15311d3ea1d4b'}}
            workflow.parent_images_digests = expected_digests
            with pytest.raises(PluginFailedException) as exc:
                test_check_base_image_plugin(workflow, SOURCE_REGISTRY, BASE_IMAGE,
                                             workflow_callback=workflow_callback,
                                             mock_get_manifest_list=False,
                                             expected_digests=expected_digests)
            assert no_manifest_msg in str(exc.value)

    def test_manifest_list_with_no_response(self, workflow):
        def workflow_callback(workflow):
            workflow = self.prepare(workflow, mock_get_manifest_list=False)
            (flexmock(atomic_reactor.util.RegistryClient)
             .should_receive('get_manifest_list')
             .and_return(None))
            return workflow

        with pytest.raises(PluginFailedException) as exc_info:
            test_check_base_image_plugin(workflow, SOURCE_REGISTRY, BASE_IMAGE,
                                         workflow_callback=workflow_callback,
                                         mock_get_manifest_list=False)
        assert 'Unable to fetch manifest list' in str(exc_info.value)

    @pytest.mark.parametrize('existing_arches, missing_arches_str', [
        # Expected arches are amd64, ppc64le
        ([], 'amd64, ppc64le'),
        (['amd64'], 'ppc64le'),
        (['ppc64le'], 'amd64'),
    ])
    def test_manifest_list_missing_arches(self, existing_arches, missing_arches_str, workflow):
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
            test_check_base_image_plugin(workflow, SOURCE_REGISTRY, BASE_IMAGE,
                                         workflow_callback=workflow_callback,
                                         mock_get_manifest_list=False)

        base_image_with_registry = 'registry.example.com/{}'.format(BASE_IMAGE)
        expected_msg = ('Base image {} not available for arches: {}'
                        .format(base_image_with_registry, missing_arches_str))
        assert expected_msg in str(exc_info.value)

    @pytest.mark.parametrize('exception', (
            HTTPError,
            RetryError,
            Timeout,
    ))
    def test_manifest_config_raises(self, exception, workflow):
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
            test_check_base_image_plugin(workflow, SOURCE_REGISTRY, BASE_IMAGE_W_SHA,
                                         workflow_callback=workflow_callback,
                                         mock_get_manifest_list=False)
        assert 'Unable to fetch config for base image' in str(exc_info.value)

    @pytest.mark.parametrize('sha_is_manifest_list', (
            True,
            False,
    ))
    def test_manifest_config_passes(self, sha_is_manifest_list, workflow):
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
                manifest_tag = '{}/{}:{}'. \
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

        test_check_base_image_plugin(workflow, SOURCE_REGISTRY, BASE_IMAGE_W_SHA,
                                     workflow_callback=workflow_callback,
                                     mock_get_manifest_list=False)

    @pytest.mark.parametrize('fail', (True, False))
    def test_parent_images_digests(self, caplog, workflow, fail):
        """Testing processing of parent_images_digests"""

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
                workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = {'x86_64'}

            test_vals['workflow'] = workflow
            return workflow

        if fail:
            with pytest.raises(PluginFailedException) as exc:
                test_check_base_image_plugin(workflow, SOURCE_REGISTRY, BASE_IMAGE,
                                             workflow_callback=workflow_callback,
                                             mock_get_manifest_list=False)
            assert 'not available for arches' in str(exc.value)
        else:
            test_check_base_image_plugin(workflow, SOURCE_REGISTRY, BASE_IMAGE,
                                         workflow_callback=workflow_callback)

            replacing_msg = ("Replacing image '{}/{}' with '{}@sha256:{}'"
                             .format(SOURCE_REGISTRY, BASE_IMAGE, reg_image_no_tag,
                                     manifest_list_digest))
            assert replacing_msg in caplog.text

            # check if worker.builder has set correct values
            builder_digests_dict = test_vals['workflow'].parent_images_digests
            assert builder_digests_dict == test_vals['expected_digest']

    def prepare(self, workflow, mock_get_manifest_list=False):
        # Setup expected platforms
        env = (
            MockEnv(workflow).set_orchestrator_platforms(['x86_64', 'ppc64le'])
            .set_check_platforms_result({'x86_64', 'ppc64le'})
            .set_reactor_config(
                {'version': 1,
                 'source_registry': {'url': SOURCE_REGISTRY, 'insecure': True},
                 'platform_descriptors': [{'platform': 'x86_64',
                                           'architecture': 'amd64'}]}
            )
        )

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

        return env.workflow
