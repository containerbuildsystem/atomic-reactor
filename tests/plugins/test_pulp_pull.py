"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.constants import (PLUGIN_GROUP_MANIFESTS_KEY,
                                      PLUGIN_CHECK_AND_SET_PLATFORMS_KEY,
                                      MEDIA_TYPE_DOCKER_V2_SCHEMA1,
                                      MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                                      MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST)
from atomic_reactor.plugin import PostBuildPlugin, ExitPlugin
from atomic_reactor.plugins.post_pulp_pull import (PulpPullPlugin,
                                                   CraneTimeoutError)
from atomic_reactor.inner import TagConf, PushConf
from atomic_reactor.util import ImageName
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfig,
                                                       ReactorConfigPlugin,
                                                       ReactorConfigKeys,
                                                       WORKSPACE_CONF_KEY)

from flexmock import flexmock
import pytest
import requests
import json
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


class TestPostPulpPull(object):
    TEST_UNIQUE_IMAGE = 'foo:unique-tag'
    CRANE_URI = 'crane.example.com'
    EXPECTED_IMAGE = ImageName.parse('%s/%s' % (CRANE_URI, TEST_UNIQUE_IMAGE))
    EXPECTED_PULLSPEC = EXPECTED_IMAGE.to_str()

    def workflow(self, push=True, sync=True, build_process_failed=False,
                 postbuild_results=None, prebuild_results=None, expectv2schema2=False,
                 platform_descriptors=False):
        tag_conf = TagConf()
        tag_conf.add_unique_image(self.TEST_UNIQUE_IMAGE)
        push_conf = PushConf()
        if push:
            push_conf.add_pulp_registry('pulp', crane_uri=self.CRANE_URI, server_side_sync=False)
        if sync:
            push_conf.add_pulp_registry('pulp', crane_uri=self.CRANE_URI, server_side_sync=True)

        conf = {
            ReactorConfigKeys.VERSION_KEY: 1,
            'prefer_schema1_digest': not expectv2schema2
        }
        if platform_descriptors:
            conf['platform_descriptors'] = [
                {'platform': 'x86_64', 'architecture': 'amd64'},
            ]
        plugin_workspace = {
            ReactorConfigPlugin.key: {
                WORKSPACE_CONF_KEY: ReactorConfig(conf)
            }
        }

        mock_get_retry_session()
        builder = flexmock()
        setattr(builder, 'image_id', 'sha256:(old)')
        return flexmock(tag_conf=tag_conf,
                        push_conf=push_conf,
                        builder=builder,
                        build_process_failed=build_process_failed,
                        plugin_workspace=plugin_workspace,
                        postbuild_results=postbuild_results or {},
                        prebuild_results=prebuild_results or {})

    media_type_v1 = 'application/vnd.docker.distribution.manifest.v1+json'
    media_type_v2 = 'application/vnd.docker.distribution.manifest.v2+json'
    media_type_v2_list = 'application/vnd.docker.distribution.manifest.list.v2+json'

    def get_response_config_json(media_type):
        return {
            'config': {
                'digest': 'sha256:2c782e3a93d34d89ea4cf54052768be117caed54803263dd1f3798ce42aac14',
                'mediaType': 'application/octet-stream',
                'size': 4132
            },
            'layers': [
                {
                    'digest': 'sha256:16dc1f96e3a1bb628be2e00518fec2bb97bd5933859de592a00e2eb7774b',
                    'mediaType': 'application/vnd.docker.image.rootfs.diff.tar.gzip',
                    'size': 71907148
                },
                {
                    'digest': 'sha256:cebc0565e1f096016765f55fde87a6f60fdb1208c0b5017e35a856ff578f',
                    'mediaType': 'application/vnd.docker.image.rootfs.diff.tar.gzip',
                    'size': 3945724
                }
            ],
            'mediaType': media_type,
            'schemaVersion': 2
        }

    broken_response = {
        'schemaVersion': 'foo',
        'not-mediaType': 'bar'
    }

    config_response_config_v1 = requests.Response()
    (flexmock(config_response_config_v1,
              raise_for_status=lambda: None,
              status_code=requests.codes.ok,
              json=get_response_config_json(media_type_v1),
              headers={
                'Content-Type': 'application/vnd.docker.distribution.manifest.v1+json',
                'Docker-Content-Digest': DIGEST_V1
              }))

    config_response_config_v2 = requests.Response()
    (flexmock(config_response_config_v2,
              raise_for_status=lambda: None,
              status_code=requests.codes.ok,
              json=get_response_config_json(media_type_v2),
              headers={
                'Content-Type': 'application/vnd.docker.distribution.manifest.v2+json',
                'Docker-Content-Digest': DIGEST_V2
              }))

    config_response_config_v2_no_headers = requests.Response()
    (flexmock(config_response_config_v2_no_headers,
              raise_for_status=lambda: None,
              status_code=requests.codes.ok,
              _content=json.dumps(get_response_config_json(media_type_v2)).encode('utf-8'),
              headers={}))

    config_response_config_v2_broken = requests.Response()
    (flexmock(config_response_config_v2_broken,
              raise_for_status=lambda: None,
              status_code=requests.codes.ok,
              _content=json.dumps(broken_response).encode('utf-8'),
              headers={}))

    config_response_config_v2_list = requests.Response()
    (flexmock(config_response_config_v2_list,
              raise_for_status=lambda: None,
              status_code=requests.codes.ok,
              json=get_response_config_json(media_type_v2_list),
              headers={
                'Content-Type': 'application/vnd.docker.distribution.manifest.list.v2+json',
              }))

    def custom_get_v1(self, url, headers, **kwargs):
        return self.config_response_config_v1

    def custom_get_v2(self, url, headers, **kwargs):
        return self.config_response_config_v2

    def custom_get_v2_list(self, url, headers, **kwargs):
        return self.config_response_config_v2_list

    def custom_get_v2_no_headers(self, url, headers, **kwargs):
        return self.config_response_config_v2_no_headers

    def custom_get_v2_broken(self, url, headers, **kwargs):
        return self.config_response_config_v2_broken

    @pytest.mark.parametrize(('no_headers, broken_response'), [
        (True, True),
        (True, False),
        (False, False)
    ])
    @pytest.mark.parametrize('insecure', [True, False])
    @pytest.mark.parametrize(('schema_version', 'pulp_plugin', 'expected_version'), [
        ('v1', [], []),
        ('v1', [{'name': 'pulp_push'}], ['application/json']),
        ('v1', [{'name': 'pulp_sync'}],
         ['application/vnd.docker.distribution.manifest.v1+json']),
        ('v1', [{'name': 'pulp_sync'}, {'name': 'pulp_push'}],
         ['application/json',
          'application/vnd.docker.distribution.manifest.v1+json']),
        ('v2', [],
         ['application/vnd.docker.distribution.manifest.v2+json']),
        ('v2', [{'name': 'pulp_push'}],
         ['application/json',
          'application/vnd.docker.distribution.manifest.v2+json']),
        ('v2', [{'name': 'pulp_sync'}],
         ['application/vnd.docker.distribution.manifest.v1+json',
          'application/vnd.docker.distribution.manifest.v2+json']),
        ('v2', [{'name': 'pulp_sync'}, {'name': 'pulp_push'}],
         ['application/json',
          'application/vnd.docker.distribution.manifest.v1+json',
          'application/vnd.docker.distribution.manifest.v2+json']),
        ('list.v2', [],
         ['application/vnd.docker.distribution.manifest.list.v2+json']),
        ('list.v2', [{'name': 'pulp_push'}],
         ['application/json',
          'application/vnd.docker.distribution.manifest.list.v2+json']),
        ('list.v2', [{'name': 'pulp_sync'}],
         ['application/vnd.docker.distribution.manifest.list.v2+json',
          'application/vnd.docker.distribution.manifest.v1+json']),
        ('list.v2', [{'name': 'pulp_sync'}, {'name': 'pulp_push'}],
         ['application/json',
          'application/vnd.docker.distribution.manifest.list.v2+json',
          'application/vnd.docker.distribution.manifest.v1+json']),
    ])
    def test_pull_first_time(self, no_headers, broken_response, insecure, schema_version,
                             pulp_plugin, expected_version):
        workflow = self.workflow()
        tasker = MockerTasker()

        test_id = 'sha256:(new)'

        if schema_version == 'v2':
            # for v2, we just return pre-existing ID
            test_id = 'sha256:(old)'

        if schema_version == 'v1':
            getter = self.custom_get_v1
        elif schema_version == 'list.v2':
            getter = self.custom_get_v2_list
        elif no_headers:
            if broken_response:
                getter = self.custom_get_v2_broken
            else:
                getter = self.custom_get_v2_no_headers
        else:
            getter = self.custom_get_v2

        (flexmock(requests.Session)
            .should_receive('get')
            .replace_with(getter))

        if schema_version in ['v1', 'list.v2'] or broken_response:
            (flexmock(tasker)
                .should_call('pull_image')
                .with_args(self.EXPECTED_IMAGE, insecure=insecure)
                .and_return(self.EXPECTED_PULLSPEC)
                .once()
                .ordered())

            (flexmock(tasker)
                .should_receive('inspect_image')
                .with_args(self.EXPECTED_PULLSPEC)
                .and_return({'Id': test_id})
                .once())
        else:
            (flexmock(tasker)
                .should_call('pull_image')
                .never())

            (flexmock(tasker)
                .should_call('inspect_image')
                .never())

        # Convert pulp_plugin into a JSON string and back into an object
        # to make really sure we get a different string object back.
        workflow.postbuild_plugins_conf = json.loads(json.dumps(pulp_plugin))

        # Set the timeout parameters so that we retry exactly once, but quickly.
        # With the get_manifest_digests() API, the 'broken_response' case isn't
        # distinguishable from no manifest yet, so we retry until timout and then
        # fall through to pulp_pull.
        plugin = PulpPullPlugin(tasker, workflow, insecure=insecure,
                                timeout=0.1, retry_delay=0.25)
        version = plugin.run()

        if not broken_response:
            assert version == expected_version

        if schema_version == 'v1':
            assert len(tasker.pulled_images) == 1
            pulled = tasker.pulled_images[0].to_str()
            assert pulled == self.EXPECTED_PULLSPEC

        # Image ID is updated in workflow
        assert workflow.builder.image_id == test_id

    @pytest.mark.parametrize(('push', 'sync'), [
        (True, False),
        (False, True),
        (True, True)
    ])
    def test_pull_push_vs_sync(self, push, sync):
        workflow = self.workflow(push=push, sync=sync)
        tasker = MockerTasker()

        test_id = 'sha256:(new)'

        getter = self.custom_get_v1

        if sync:
            (flexmock(requests.Session)
                .should_receive('get')
                .replace_with(getter))
        else:
            (flexmock(requests.Session)
                .should_receive('get')
                .never())

        (flexmock(tasker)
            .should_call('pull_image')
            .with_args(self.EXPECTED_IMAGE, insecure=False)
            .and_return(self.EXPECTED_PULLSPEC)
            .ordered())

        (flexmock(tasker)
            .should_receive('inspect_image')
            .with_args(self.EXPECTED_PULLSPEC)
            .and_return({'Id': test_id}))

        workflow.postbuild_plugins_conf = []

        plugin = PulpPullPlugin(tasker, workflow)
        plugin.run()

        assert workflow.builder.image_id == test_id
        assert len(tasker.pulled_images) == 1

    @pytest.mark.parametrize('reactor_config', [True, False])
    @pytest.mark.parametrize('v2,expect_v2schema2', [
        (False, False),
        (False, True),
        (True, False),
        (True, True),
    ])
    @pytest.mark.parametrize('timeout,retry_delay,failures,expect_success', [
        (0.1, 0.06, 1, True),
        (0.1, 0.06, 1, True),
        (0.1, 0.06, 3, False),
    ])
    def test_pull_retry(self, expect_v2schema2, v2, timeout, retry_delay, failures,
                        expect_success, reactor_config):
        workflow = self.workflow(expect_v2schema2)
        tasker = MockerTasker()
        if v2:
            test_id = 'sha256:(old)'
        else:
            # Image ID is updated in workflow
            test_id = 'sha256:(new)'

        not_found = requests.Response()
        flexmock(not_found, status_code=requests.codes.not_found)
        expectation = flexmock(requests.Session).should_receive('get')
        # If pulp is returning a 404 for a manifest URL, we will get 5 requests
        # (for v1, v2, list.v2, oci, and oci.index media types) before get_manifest_digests
        # gives up, so we need to return 5 404's to equal one "failure".
        for _ in range(5 * failures):
            expectation = expectation.and_return(not_found)

        expectation.and_return(self.config_response_config_v1)
        if v2:
            expectation.and_return(self.config_response_config_v2)
        else:
            expectation.and_return(self.config_response_config_v1)
        expectation.and_return(self.config_response_config_v2_list)
        # No OCI support in Pulp at the moment, will return a v1 response
        expectation.and_return(self.config_response_config_v1)

        # A special case for retries - schema 2 manifest digest is expected,
        # but its never being sent - the test should fail on timeout
        if not v2 and expect_v2schema2:
            expect_success = False

        expectation = flexmock(tasker).should_call('pull_image')
        if v2:
            expectation.never()
        elif expect_success:
            expectation.and_return(self.EXPECTED_PULLSPEC).once()

        expectation = flexmock(tasker).should_receive('inspect_image')
        if v2:
            expectation.never()
        elif expect_success:
            (expectation
             .with_args(self.EXPECTED_PULLSPEC)
             .and_return({'Id': test_id})
             .once())
        workflow.postbuild_plugins_conf = []

        if reactor_config:
            plugin = PulpPullPlugin(tasker, workflow, timeout=timeout,
                                    retry_delay=retry_delay)
        else:
            plugin = PulpPullPlugin(tasker, workflow, timeout=timeout,
                                    retry_delay=retry_delay,
                                    expect_v2schema2=expect_v2schema2)

        if not expect_success:
            with pytest.raises(Exception):
                plugin.run()
            return

        plugin.run()

        assert len(tasker.pulled_images) == 0 if v2 else 1
        if not v2:
            img = tasker.pulled_images[0].to_str()
            assert img == self.EXPECTED_PULLSPEC

        assert workflow.builder.image_id == test_id

    def test_plugin_type(self):
        # arrangement versions < 4
        assert issubclass(PulpPullPlugin, PostBuildPlugin)

        # arrangement version >= 4
        assert issubclass(PulpPullPlugin, ExitPlugin)

        # Verify the plugin does nothing when running as an exit
        # plugin for an already-failed build
        workflow = self.workflow(build_process_failed=True)
        tasker = MockerTasker()
        workflow.postbuild_plugins_conf = []
        flexmock(requests.Session).should_receive('get').never()
        flexmock(tasker).should_receive('pull_image').never()
        flexmock(tasker).should_receive('inspect_image').never()
        plugin = PulpPullPlugin(tasker, workflow)
        media_types = plugin.run()
        assert len(media_types) == 0

    def test_unexpected_response(self):
        workflow = self.workflow()
        tasker = MockerTasker()
        unauthorized = requests.Response()
        flexmock(unauthorized, status_code=requests.codes.unauthorized)
        flexmock(requests.Session).should_receive('get').and_return(unauthorized)
        workflow.postbuild_plugins_conf = []
        plugin = PulpPullPlugin(tasker, workflow)
        with pytest.raises(requests.exceptions.HTTPError):
            plugin.run()

    def test_forbidden_response(self):
        workflow = self.workflow()
        tasker = MockerTasker()
        forbidden = requests.Response()
        flexmock(forbidden,
                 status_code=requests.codes.forbidden,
                 request=requests.Request(url='https://crane.example.com'))
        expectation = flexmock(requests.Session).should_receive('get')
        expectation.and_return(forbidden)
        expectation.and_return(self.config_response_config_v1)
        expectation.and_return(self.config_response_config_v2)
        expectation.and_return(self.config_response_config_v2_list)
        # No OCI support in Pulp at the moment, will return a v1 response
        expectation.and_return(self.config_response_config_v1)
        expectation.and_return(self.config_response_config_v1)
        workflow.postbuild_plugins_conf = []
        plugin = PulpPullPlugin(tasker, workflow, timeout=0.1,
                                retry_delay=0.06,
                                expect_v2schema2=True)

        plugin.run()

    @responses.activate
    @pytest.mark.parametrize(('group', 'has_v2list', 'expect_success'), [
        (False, False, True),
        (False, True, True),
        (True, False, False),
        (True, True, True),
    ])
    def test_expectations(self, group, has_v2list, expect_success):
        def get_manifest(request):
            media_types = request.headers.get('Accept', '').split(',')
            content_type = media_types[0]
            v2_content_type = 'application/vnd.docker.distribution.manifest.list.v2+json'
            if (not has_v2list) and v2_content_type in media_types:
                content_type = 'application/vnd.docker.distribution.manifest.v2+json'

            return (200, {'Content-Type': content_type}, '{}')

        url = re.compile(r'.*//crane.example.com/v2/.*/manifests/.*')
        responses.add_callback(responses.GET, url, callback=get_manifest)
        digests = {'digest': None} if group else {}
        workflow = self.workflow(postbuild_results={
            PLUGIN_GROUP_MANIFESTS_KEY: digests,
        })
        workflow.postbuild_plugins_conf = [{'name': 'pulp_sync'}]
        tasker = MockerTasker()
        plugin = PulpPullPlugin(tasker, workflow, timeout=0.2,
                                retry_delay=0.1, expect_v2schema2=True)
        if expect_success:
            plugin.run()
        else:
            with pytest.raises(CraneTimeoutError):
                plugin.run()

    @responses.activate
    @pytest.mark.parametrize(('platforms', 'platform_descriptors', 'manifest_list_only'), [
        (['ppc64le'], True, True),
        (['ppc64le'], False, False),
        (['ppc64le', 'arm'], True, True),
        (['ppc64le', 'arm'], False, False),
        (['ppc64le', 'x86_64'], True, False),
        (['ppc64le', 'x86_64'], False, False),
        (['x86_64'], True, False),
        (['x86_64'], False, False),
    ])
    def test_expect_v2schema2list_only(self, platforms, platform_descriptors, manifest_list_only):
        def get_manifest(request):
            media_types = request.headers.get('Accept', '').split(',')
            content_type = media_types[0]
            return (200, {'Content-Type': content_type}, '{}')

        url = re.compile(r'.*//crane.example.com/v2/.*/manifests/.*')
        responses.add_callback(responses.GET, url, callback=get_manifest)
        digests = {'digest': None}
        workflow = self.workflow(
            platform_descriptors=platform_descriptors,
            postbuild_results={PLUGIN_GROUP_MANIFESTS_KEY: digests},
            prebuild_results={PLUGIN_CHECK_AND_SET_PLATFORMS_KEY: set(platforms)},
        )
        workflow.postbuild_plugins_conf = [{'name': 'pulp_sync'}]
        tasker = MockerTasker()
        plugin = PulpPullPlugin(tasker, workflow, timeout=0.2,
                                retry_delay=0.1, expect_v2schema2=True)
        media_types = plugin.run()

        expected_media_types = [MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST]
        if not manifest_list_only:
            expected_media_types.append(MEDIA_TYPE_DOCKER_V2_SCHEMA1)
            expected_media_types.append(MEDIA_TYPE_DOCKER_V2_SCHEMA2)
        assert set(media_types) == set(expected_media_types)
