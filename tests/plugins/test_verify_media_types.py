"""
Copyright (c) 2018, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.constants import (PLUGIN_GROUP_MANIFESTS_KEY,
                                      PLUGIN_CHECK_AND_SET_PLATFORMS_KEY,
                                      PLUGIN_FETCH_SOURCES_KEY,
                                      MEDIA_TYPE_DOCKER_V2_SCHEMA1,
                                      MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                                      MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST,
                                      MEDIA_TYPE_OCI_V1, MEDIA_TYPE_OCI_V1_INDEX)
from atomic_reactor.plugins.exit_verify_media_types import VerifyMediaTypesPlugin
from atomic_reactor.inner import TagConf, PushConf
from atomic_reactor.auth import HTTPRegistryAuth
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfig,
                                                       ReactorConfigPlugin,
                                                       ReactorConfigKeys,
                                                       WORKSPACE_CONF_KEY)
from osbs.utils import RegistryURI

from flexmock import flexmock
import pytest
import requests
import responses
import re

from tests.constants import MOCK
if MOCK:
    from tests.retry_mock import mock_get_retry_session

DIGEST_V1 = 'sha256:7de72140ec27a911d3f88d60335f08d6530a4af136f7beab47797a196e840afd'
DIGEST_V2 = 'sha256:85a7e3fb684787b86e64808c5b91d926afda9d6b35a0642a72d7a746452e71c1'


class MockerTasker(object):
    def __init__(self):
        self.pulled_images = []

    def pull_image(self, image, insecure):
        self.pulled_images.append(image)
        return image.to_str()

    def inspect_image(self, image):
        pass


class TestVerifyImageTypes(object):
    TEST_UNIQUE_IMAGE = 'foo:unique-tag'

    def get_response_config_json(media_type):
        config = {
            'digest': 'sha256:2c782e3a93d34d89ea4cf54052768be117caed54803263dd1f3798ce42aac14',
            'mediaType': 'application/octet-stream',
            'size': 4132
        }
        layer1 = {
            'digest': 'sha256:16dc1f96e3a1bb628be2e00518fec2bb97bd5933859de592a00e2eb7774b',
            'mediaType': 'application/vnd.docker.image.rootfs.diff.tar.gzip',
            'size': 71907148
        }
        layer2 = {
            'digest': 'sha256:cebc0565e1f096016765f55fde87a6f60fdb1208c0b5017e35a856ff578f',
            'mediaType': 'application/vnd.docker.image.rootfs.diff.tar.gzip',
            'size': 3945724
        }
        return {
            'config': config,
            'layers': [layer1, layer2],
            'mediaType': media_type,
            'schemaVersion': 2
        }

    broken_response = {
        'schemaVersion': 'foo',
        'not-mediaType': 'bar'
    }

    config_response_none = requests.Response()
    (flexmock(config_response_none,
              raise_for_status=lambda: None,
              status_code=requests.codes.ok,
              json=get_response_config_json(MEDIA_TYPE_DOCKER_V2_SCHEMA2),
              headers={
                'Content-Type': 'application/invalid+json',
                'Docker-Content-Digest': "12"
              }))
    config_response_config_v1 = requests.Response()
    (flexmock(config_response_config_v1,
              raise_for_status=lambda: None,
              status_code=requests.codes.ok,
              json=get_response_config_json(MEDIA_TYPE_DOCKER_V2_SCHEMA1),
              headers={
                'Content-Type': MEDIA_TYPE_DOCKER_V2_SCHEMA1,
                'Docker-Content-Digest': DIGEST_V1
              }))
    config_response_config_v2 = requests.Response()
    (flexmock(config_response_config_v2,
              raise_for_status=lambda: None,
              status_code=requests.codes.ok,
              json=get_response_config_json(MEDIA_TYPE_DOCKER_V2_SCHEMA2),
              headers={
                'Content-Type': MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                'Docker-Content-Digest': DIGEST_V2
              }))
    config_response_config_v2_list = requests.Response()
    (flexmock(config_response_config_v2_list,
              raise_for_status=lambda: None,
              status_code=requests.codes.ok,
              json=get_response_config_json(MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST),
              headers={
                'Content-Type': MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST,
              }))
    config_response_config_oci_v1 = requests.Response()
    (flexmock(config_response_config_oci_v1,
              raise_for_status=lambda: None,
              status_code=requests.codes.ok,
              json=get_response_config_json(MEDIA_TYPE_OCI_V1),
              headers={
                'Content-Type': MEDIA_TYPE_OCI_V1,
              }))
    config_response_config_oci_v1_index = requests.Response()
    (flexmock(config_response_config_oci_v1_index,
              raise_for_status=lambda: None,
              status_code=requests.codes.ok,
              json=get_response_config_json(MEDIA_TYPE_OCI_V1_INDEX),
              headers={
                'Content-Type': MEDIA_TYPE_OCI_V1_INDEX,
              }))

    def workflow(self, build_process_failed=False, registries=None, registry_types=None,
                 platforms=None, platform_descriptors=None, group=True, fail=False,
                 limit_media_types=None):
        tag_conf = TagConf()
        tag_conf.add_unique_image(self.TEST_UNIQUE_IMAGE)

        push_conf = PushConf()

        if platform_descriptors is None:
            platform_descriptors = [
                {'platform': 'x86_64', 'architecture': 'amd64'},
                {'platform': 'ppc64le', 'architecture': 'ppc64le'},
                {'platform': 's390x', 'architecture': 's390x'},
            ]

        if platforms is None:
            platforms = [descriptor['platform'] for descriptor in platform_descriptors]
        no_amd64 = 'x86_64' not in platforms

        keep_types = False
        if registries or registry_types:
            keep_types = True

        if registries is None and registry_types is None:
            registry_types = [MEDIA_TYPE_DOCKER_V2_SCHEMA1,
                              MEDIA_TYPE_DOCKER_V2_SCHEMA2, MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST,
                              MEDIA_TYPE_OCI_V1, MEDIA_TYPE_OCI_V1_INDEX]

        if registries is None:
            registries = [{
                'url': 'https://container-registry.example.com/v2',
                'version': 'v2',
                'insecure': True,
                'expected_media_types': registry_types
            }]
        conf = {
            ReactorConfigKeys.VERSION_KEY: 1,
            'registries': registries,
        }

        if limit_media_types is not None:
            conf['source_container'] = {
                'limit_media_types': limit_media_types,
            }

        if platform_descriptors:
            conf['platform_descriptors'] = platform_descriptors

        plugin_workspace = {
            ReactorConfigPlugin.key: {
                WORKSPACE_CONF_KEY: ReactorConfig(conf)
            }
        }

        flexmock(HTTPRegistryAuth).should_receive('__new__').and_return(None)
        mock_auth = None
        for registry in registries:
            def get_manifest(request):
                media_types = request.headers.get('Accept', '').split(',')
                content_type = media_types[0]

                return (200, {'Content-Type': content_type}, '{}')

            url_regex = "r'" + registry['url'] + ".*/manifests/.*'"
            url = re.compile(url_regex)
            responses.add_callback(responses.GET, url, callback=get_manifest)

            expected_types = registry.get('expected_media_types', registry_types or [])
            if fail == "bad_results":
                response_types = []
            elif not keep_types and no_amd64:
                response_types = [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]
            else:
                response_types = expected_types

            reguri = RegistryURI(registry['url']).docker_uri
            if re.match('http(s)?://', reguri):
                urlbase = reguri
            else:
                urlbase = 'https://{0}'.format(reguri)

            actual_v2_url = urlbase + "/v2/foo/manifests/unique-tag"

            if fail == "bad_results":
                response = requests.Response()
                (flexmock(response,
                          raise_for_status=lambda: None,
                          status_code=requests.codes.ok,
                          json={},
                          headers={'Content-Type': 'application/json'}))
                v1_response = response
                v1_oci_response = response
                v1_oci_index_response = response
                v2_response = response
                v2_list_response = response
            else:
                v1_response = self.config_response_none
                v1_oci_response = self.config_response_none
                v1_oci_index_response = self.config_response_none
                v2_response = self.config_response_none
                v2_list_response = self.config_response_none

            if MEDIA_TYPE_DOCKER_V2_SCHEMA1 in response_types:
                v1_response = self.config_response_config_v1
            if MEDIA_TYPE_DOCKER_V2_SCHEMA2 in response_types:
                v2_response = self.config_response_config_v2
            if MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST in response_types:
                v2_list_response = self.config_response_config_v2_list
            if MEDIA_TYPE_OCI_V1 in response_types:
                v1_oci_response = self.config_response_config_oci_v1
            if MEDIA_TYPE_OCI_V1_INDEX in response_types:
                v1_oci_index_response = self.config_response_config_oci_v1_index

            v2_header_v1 = {'Accept': MEDIA_TYPE_DOCKER_V2_SCHEMA1}
            v2_header_v2 = {'Accept': MEDIA_TYPE_DOCKER_V2_SCHEMA2}
            manifest_header = {'Accept': MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST}

            (flexmock(requests.Session)
                .should_receive('get')
                .with_args(actual_v2_url, headers=v2_header_v1,
                           auth=mock_auth, verify=False)
                .and_return(v1_response))
            (flexmock(requests.Session)
                .should_receive('get')
                .with_args(actual_v2_url, headers=v2_header_v2,
                           auth=mock_auth, verify=False)
                .and_return(v2_response))
            (flexmock(requests.Session)
                .should_receive('get')
                .with_args(actual_v2_url, headers={'Accept': MEDIA_TYPE_OCI_V1},
                           auth=mock_auth, verify=False)
                .and_return(v1_oci_response))
            (flexmock(requests.Session)
                .should_receive('get')
                .with_args(actual_v2_url, headers={'Accept': MEDIA_TYPE_OCI_V1_INDEX},
                           auth=mock_auth, verify=False)
                .and_return(v1_oci_index_response))
            (flexmock(requests.Session)
                .should_receive('get')
                .with_args(actual_v2_url, headers=manifest_header,
                           auth=mock_auth, verify=False)
                .and_return(v2_list_response))

        digests = {'media_type': MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST}
        if not group:
            digests = {'media_type': MEDIA_TYPE_DOCKER_V2_SCHEMA2}
        prebuild_results = {PLUGIN_CHECK_AND_SET_PLATFORMS_KEY: platforms}
        postbuild_results = {PLUGIN_GROUP_MANIFESTS_KEY: digests}

        mock_get_retry_session()
        builder = flexmock()
        setattr(builder, 'image_id', 'sha256:(old)')
        return flexmock(tag_conf=tag_conf,
                        push_conf=push_conf,
                        builder=builder,
                        build_process_failed=build_process_failed,
                        plugin_workspace=plugin_workspace,
                        prebuild_results=prebuild_results,
                        postbuild_results=postbuild_results)

    @responses.activate
    def test_verify_successful_simple(self):
        """
        The simplest test case, and everything works
        """
        workflow = self.workflow()
        tasker = MockerTasker()

        # Set the timeout parameters so that we retry exactly once, but quickly.
        # With the get_manifest_digests() API, the 'broken_response' case isn't
        # distinguishable from no manifest yet, so we retry until timout
        plugin = VerifyMediaTypesPlugin(tasker, workflow)
        results = plugin.run()

        assert results == sorted([MEDIA_TYPE_DOCKER_V2_SCHEMA1,
                                  MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                                  MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST,
                                  MEDIA_TYPE_OCI_V1,
                                  MEDIA_TYPE_OCI_V1_INDEX])

    @responses.activate
    @pytest.mark.parametrize(('registry_types', 'platform_descriptors',
                              'group', 'expected_results'), [
        ([],
         [{'platform': 'arm64', 'architecture': 'arm64'}],
         True, [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]),
        # If group manifests ran, non-x86-64 builds can only produce
        # MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST
        ([MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST],
         [{'platform': 'arm64', 'architecture': 'arm64'}],
         True, [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]),
        ([MEDIA_TYPE_DOCKER_V2_SCHEMA2, MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST],
         [],  # no platforms
         True, [MEDIA_TYPE_DOCKER_V2_SCHEMA2, MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]),
        ([MEDIA_TYPE_DOCKER_V2_SCHEMA1, MEDIA_TYPE_DOCKER_V2_SCHEMA2,
          MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST],
         [{'platform': 'arm64', 'architecture': 'arm64'}],
         True, [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]),
        ([MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST],
         [{'platform': 'x86_64', 'architecture': 'amd64'},
          {'platform': 'arm64', 'architecture': 'arm64'}],
         True, [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]),
        ([MEDIA_TYPE_DOCKER_V2_SCHEMA2, MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST],
         [{'platform': 'x86_64', 'architecture': 'amd64'},
          {'platform': 'arm64', 'architecture': 'arm64'}],
         True, [MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]),
        # If group manifests didn't run, non-x86-64 builds can produce any type
        # Well, actually, the build will fail but if it didn't fail, they could produce any type
        ([MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST],
         [{'platform': 'arm64', 'architecture': 'arm64'}],
         False, [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]),
        ([MEDIA_TYPE_DOCKER_V2_SCHEMA1,
          MEDIA_TYPE_DOCKER_V2_SCHEMA2, MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST],
         [{'platform': 'arm64', 'architecture': 'arm64'}],
         False, [MEDIA_TYPE_DOCKER_V2_SCHEMA1,
                 MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                 MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]),
        ([MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST],
         [{'platform': 'x86_64', 'architecture': 'amd64'},
          {'platform': 'arm64', 'architecture': 'arm64'}],
         False, [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]),
        ([MEDIA_TYPE_DOCKER_V2_SCHEMA2, MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST],
         [{'platform': 'x86_64', 'architecture': 'amd64'},
          {'platform': 'arm64', 'architecture': 'arm64'}],
         False, [MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                 MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]),
    ])
    def test_verify_successful_complicated(self, registry_types,
                                           platform_descriptors, group, expected_results):
        workflow = self.workflow(registry_types=registry_types,
                                 platform_descriptors=platform_descriptors, group=group)
        tasker = MockerTasker()

        plugin = VerifyMediaTypesPlugin(tasker, workflow)
        results = plugin.run()

        assert results == sorted(expected_results)

    @responses.activate
    @pytest.mark.parametrize(('registries', 'platform_descriptors', 'group',
                              'expected_results'), [
        ([{'url': 'https://container-registry.example.com/v2',
           'version': 'v2', 'insecure': True,
           'expected_media_types': [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]},
          {'url': 'https://container-registry-test.example.com/v2',
           'version': 'v2', 'insecure': True,
           'expected_media_types': [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]}],
         [{'platform': 'arm64', 'architecture': 'arm64'}],
         True, [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]),
        ([{'url': 'https://container-registry.example.com/v2',
           'version': 'v2', 'insecure': True,
           'expected_media_types': [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]},
          {'url': 'https://container-registry-test.example.com/v2',
           'version': 'v2', 'insecure': True,
           'expected_media_types': [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]}],
         [{'platform': 'arm64', 'architecture': 'arm64'}],
         False, [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]),
        ([{'url': 'https://container-registry.example.com/v2',
           'version': 'v2', 'insecure': True,
           'expected_media_types': [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]},
          {'url': 'https://container-registry-test.example.com/v2',
           'version': 'v2', 'insecure': True,
           'expected_media_types': []}],
         [{'platform': 'arm64', 'architecture': 'arm64'}],
         False, [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]),
        ([{'url': 'https://container-registry.example.com/v2',
           'version': 'v2', 'insecure': True,
           'expected_media_types': [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]},
          {'url': 'https://container-registry-test.example.com/v2',
           'version': 'v2', 'insecure': True,
           'expected_media_types': [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]}],
         [{'platform': 'arm64', 'architecture': 'arm64'},
          {'platform': 'x86_64', 'architecture': 'amd64'}],
         True, [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]),
    ])
    def test_verify_successful_two_registries(self, registries,
                                              platform_descriptors, group, expected_results):
        """
        Two registries, everything behaves correctly
        """
        workflow = self.workflow(registries=registries,
                                 platform_descriptors=platform_descriptors, group=group)
        tasker = MockerTasker()

        plugin = VerifyMediaTypesPlugin(tasker, workflow)
        results = plugin.run()
        assert results == sorted(expected_results)

    @responses.activate
    @pytest.mark.parametrize(('registries', 'platforms', 'platform_descriptors',
                              'group', 'expected_results'), [
        # Null registries and registries without expected_media_types return nothing
        ([],
         None, [{'platform': 'x86_64', 'architecture': 'amd64'}],
         True, []),
        ([{'url': 'https://container-registry.example.com/v2',
           'version': 'v2', 'insecure': True}],
         None, [{'platform': 'x86_64', 'architecture': 'amd64'}],
         True, []),

        ([{'url': 'https://container-registry.example.com/v2',
           'version': 'v2', 'insecure': True},
          {'url': 'https://container-registry-test.example.com/v2',
           'version': 'v2', 'insecure': True,
           'expected_media_types': [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]}],
         None, [{'platform': 'arm64', 'architecture': 'arm64'}],
         False, [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]),

        # no platforms or platform descriptors, assume x86_64 wasn't build
        ([{'url': 'https://container-registry.example.com/v2',
           'version': 'v2', 'insecure': True,
           'expected_media_types': [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]}],
         ['x86_64', 'arm64'], [],
         True, [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]),
    ])
    def test_verify_malformed_two_registries(self, registries, platforms,
                                             platform_descriptors, group, expected_results):
        """
        Configuration is bad, but not so bad as to cause a problem
        """
        workflow = self.workflow(registries=registries, platforms=platforms,
                                 platform_descriptors=platform_descriptors, group=group)
        tasker = MockerTasker()

        plugin = VerifyMediaTypesPlugin(tasker, workflow)
        results = plugin.run()

        assert results == sorted(expected_results)

    @responses.activate
    def test_verify_fail_no_image(self):
        """
        If there is no image, this plugin shouldn't run and how did we get here?
        """
        workflow = self.workflow()
        workflow.tag_conf = TagConf()
        tasker = MockerTasker()

        plugin = VerifyMediaTypesPlugin(tasker, workflow)
        with pytest.raises(ValueError) as exc:
            plugin.run()
        assert "no unique image set, impossible to verify media types" in str(exc.value)

    @responses.activate
    def test_verify_fail_no_build(self):
        """
        Build was unsuccessful, return an empty list
        """
        workflow = self.workflow(build_process_failed=True)
        tasker = MockerTasker()

        plugin = VerifyMediaTypesPlugin(tasker, workflow)
        results = plugin.run()
        assert results == []

    @responses.activate
    def test_verify_fail_bad_results(self, caplog):
        """
        All results are garbage, so fail
        """
        workflow = self.workflow(fail="bad_results")
        tasker = MockerTasker()

        plugin = VerifyMediaTypesPlugin(tasker, workflow)
        expect_media_types = []
        expect_missing_types = sorted([MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST,
                                       MEDIA_TYPE_DOCKER_V2_SCHEMA1,
                                       MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                                       MEDIA_TYPE_OCI_V1,
                                       MEDIA_TYPE_OCI_V1_INDEX])

        expected_msg = "expected media types {0} ".format(expect_missing_types)
        available_msg = "not in available media types {0}, ".format(expect_media_types)
        registry_msg = "for registry {0}".format('container-registry.example.com')

        failmsg = expected_msg + available_msg + registry_msg

        with pytest.raises(KeyError) as exc:
            plugin.run()
        assert 'expected media types were not found' in str(exc.value)
        assert failmsg in caplog.text

    @responses.activate
    @pytest.mark.parametrize('expected_media_types', [
        None,
        [MEDIA_TYPE_DOCKER_V2_SCHEMA1, MEDIA_TYPE_DOCKER_V2_SCHEMA2],
    ])
    def test_source_container(self, expected_media_types):
        """
        Test that v2 schema 1 images are not reported for source containers
        """
        registry = {
            'url': 'https://container-registry.example.com/v2',
            'version': 'v2',
            'insecure': True,
        }

        if expected_media_types is not None:
            registry['expected_media_types'] = expected_media_types

        workflow = self.workflow(registries=[registry], group=False,
                                 limit_media_types=[
                                     MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                                 ],
                                 registry_types=[
                                     MEDIA_TYPE_DOCKER_V2_SCHEMA1,
                                     MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                                 ])
        workflow.prebuild_results[PLUGIN_FETCH_SOURCES_KEY] = {}
        tasker = MockerTasker()

        plugin = VerifyMediaTypesPlugin(tasker, workflow)
        results = plugin.run()
        assert results == [MEDIA_TYPE_DOCKER_V2_SCHEMA2]
