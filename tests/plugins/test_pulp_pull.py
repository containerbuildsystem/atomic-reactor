"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.plugins.post_pulp_pull import (PulpPullPlugin,
                                                   CraneTimeoutError)
from atomic_reactor.inner import TagConf, PushConf
from atomic_reactor.util import ImageName
from docker.errors import NotFound

from flexmock import flexmock
import pytest
import requests
import json

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

    def workflow(self):
        tag_conf = TagConf()
        tag_conf.add_unique_image(self.TEST_UNIQUE_IMAGE)
        push_conf = PushConf()
        push_conf.add_pulp_registry('pulp', crane_uri=self.CRANE_URI)
        builder = flexmock()
        setattr(builder, 'image_id', 'sha256:(old)')
        return flexmock(tag_conf=tag_conf,
                        push_conf=push_conf,
                        builder=builder,
                        plugin_workspace={})

    media_type_v1 = 'application/vnd.docker.distribution.manifest.v1+json'
    media_type_v2 = 'application/vnd.docker.distribution.manifest.v2+json'

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

    def custom_get_v1(self, url, headers, **kwargs):
        return self.config_response_config_v1

    def custom_get_v2(self, url, headers, **kwargs):
        return self.config_response_config_v2

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
        elif no_headers:
            if broken_response:
                getter = self.custom_get_v2_broken
            else:
                getter = self.custom_get_v2_no_headers
        else:
            getter = self.custom_get_v2

        (flexmock(requests)
            .should_receive('get')
            .replace_with(getter))

        if schema_version == 'v1' or broken_response:
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

        plugin = PulpPullPlugin(tasker, workflow, insecure=insecure)
        results, version = plugin.run()

        # Plugin return value is the new ID and schema
        assert results == test_id
        if not broken_response:
            assert version == expected_version

        if schema_version == 'v1':
            assert len(tasker.pulled_images) == 1
            pulled = tasker.pulled_images[0].to_str()
            assert pulled == self.EXPECTED_PULLSPEC

        # Image ID is updated in workflow
        assert workflow.builder.image_id == test_id

    def test_pull_timeout(self):
        workflow = self.workflow()
        tasker = MockerTasker()

        (flexmock(requests)
            .should_receive('get')
            .replace_with(self.custom_get_v1))

        (flexmock(tasker)
            .should_call('pull_image')
            .and_return(self.EXPECTED_PULLSPEC)
            .times(3))

        (flexmock(tasker)
            .should_receive('inspect_image')
            .with_args(self.EXPECTED_PULLSPEC)
            .and_raise(NotFound('message', flexmock(content=None)))
            .times(3))
        workflow.postbuild_plugins_conf = []

        plugin = PulpPullPlugin(tasker, workflow, timeout=1, retry_delay=0.6)

        # Should raise a timeout exception
        with pytest.raises(CraneTimeoutError):
            plugin.run()

    def test_pull_retry(self):
        workflow = self.workflow()
        tasker = MockerTasker()
        test_id = 'sha256:(new)'

        (flexmock(requests)
            .should_receive('get')
            .replace_with(self.custom_get_v1))

        (flexmock(tasker)
            .should_call('pull_image')
            .and_return(self.EXPECTED_PULLSPEC)
            .times(3))

        (flexmock(tasker)
            .should_receive('inspect_image')
            .with_args(self.EXPECTED_PULLSPEC)
            .and_raise(NotFound('message', flexmock(content=None)))
            .and_raise(NotFound('message', flexmock(content=None)))
            .and_return({'Id': test_id})
            .times(3))
        workflow.postbuild_plugins_conf = []

        plugin = PulpPullPlugin(tasker, workflow, timeout=1, retry_delay=0.6)

        # Plugin return value is the new ID and schema
        results, version = plugin.run()

        # Plugin return value is the new ID
        assert results == test_id

        assert len(tasker.pulled_images) == 3
        for image in tasker.pulled_images:
            pulled = image.to_str()
            assert pulled == self.EXPECTED_PULLSPEC

        # Image ID is updated in workflow
        assert workflow.builder.image_id == test_id
