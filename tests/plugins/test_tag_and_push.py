"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import pytest
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner
from atomic_reactor.plugins.post_tag_and_push import TagAndPushPlugin
from atomic_reactor.util import ImageName, ManifestDigest
from tests.constants import LOCALHOST_REGISTRY, TEST_IMAGE, INPUT_IMAGE, MOCK, DOCKER0_REGISTRY
import atomic_reactor.util

import json
import os.path
from tempfile import mkdtemp
import requests

if MOCK:
    import docker
    from flexmock import flexmock
    from tests.docker_mock import mock_docker

DIGEST_V1 = 'sha256:7de72140ec27a911d3f88d60335f08d6530a4af136f7beab47797a196e840afd'
DIGEST_V2 = 'sha256:85a7e3fb684787b86e64808c5b91d926afda9d6b35a0642a72d7a746452e71c1'

DIGEST_LOG = 'sha256:hey-this-should-not-be-used'
PUSH_LOGS_1_10 = [
    b'{"status":"The push refers to a repository [localhost:5000/busybox]"}',
    b'{"status":"Preparing","progressDetail":{},"id":"5f70bf18a086"}',
    b'{"status":"Preparing","progressDetail":{},"id":"9508eff2c687"}',
    b'{"status":"Pushing","progressDetail":{"current":721920,"total":1113436},"progress":"[================================\\u003e                  ] 721.9 kB/1.113 MB","id":"9508eff2c687"}',
    b'{"status":"Pushing","progressDetail":{"current":1024},"progress":"1.024 kB","id":"5f70bf18a086"}',
    b'{"status":"Pushing","progressDetail":{"current":820224,"total":1113436},"progress":"[====================================\\u003e              ] 820.2 kB/1.113 MB","id":"9508eff2c687"}',
    b'{"status":"Pushed","progressDetail":{},"id":"5f70bf18a086"}',
    b'{"status":"Pushed","progressDetail":{},"id":"5f70bf18a086"}',
    b'{"status":"Pushing","progressDetail":{"current":1300992,"total":1113436},"progress":"[==================================================\\u003e] 1.301 MB","id":"9508eff2c687"}',
    b'{"status":"Pushing","progressDetail":{"current":1310720,"total":1113436},"progress":"[==================================================\\u003e] 1.311 MB","id":"9508eff2c687"}',
    b'{"status":"Pushed","progressDetail":{},"id":"9508eff2c687"}',
    b'{"status":"Pushed","progressDetail":{},"id":"9508eff2c687"}',
    b'{"status":"latest: digest: ' + DIGEST_LOG.encode('utf-8') + b' size: 1920"}',
    b'{"progressDetail":{},"aux":{"Tag":"latest","Digest":"' + DIGEST_LOG.encode('utf-8') + b'","Size":1920}}' ]

PUSH_LOGS_1_10_NOT_IN_STATUS = list(PUSH_LOGS_1_10)
del PUSH_LOGS_1_10_NOT_IN_STATUS[-2]

PUSH_LOGS_1_9 = [
    b'{"status":"The push refers to a repository [172.17.42.1:5000/ns/test-image2] (len: 1)"}',
    b'{"status":"Buffering to Disk","progressDetail":{},"id":"83bca0dcfd1b"}',
    b'{"status":"Pushing","progressDetail":{"current":1,"total":32},"progress":"[=\\u003e                                                 ]      1 B/32 B","id":"83bca0dcfd1b"}',
    b'{"status":"Pushing","progressDetail":{"current":66813953,"total":66944370},"progress":"[=================================================\\u003e ] 66.81 MB/66.94 MB","id":"ded7cd95e059"}',
    b'{"status":"Pushing","progressDetail":{"current":66944370,"total":66944370},"progress":"[==================================================\\u003e] 66.94 MB/66.94 MB","id":"ded7cd95e059"}',
    b'{"status":"Image successfully pushed","progressDetail":{},"id":"ded7cd95e059"}',
    b'{"status":"Image already exists","progressDetail":{},"id":"48ecf305d2cf"}',
    b'{"status":"Digest: ' + DIGEST_LOG.encode('utf-8') + b'"}']

PUSH_LOGS_1_X = [ # don't remember which version does this
    b'{"status":"The push refers to a repository [172.17.42.1:5000/ns/test-image2]"}',
    b'{"status":"13cde7f2a483: Pushed "}',
    b'{"status":"7.1-23: digest: ' + DIGEST_LOG.encode('utf-8') + b' size: 1539"}']

PUSH_ERROR_LOGS = [
    b'{"status":"The push refers to a repository [xyz/abc] (len: 1)"}\r\n',
    b'{"errorDetail":{"message":"error message detail"},"error":"error message"}',
]


class Y(object):
    pass


class X(object):
    image_id = INPUT_IMAGE
    source = Y()
    source.dockerfile_path = None
    source.path = None
    base_image = ImageName(repo="qwe", tag="asd")

@pytest.mark.parametrize("use_secret", [
    True,
    False,
])
@pytest.mark.parametrize(("image_name", "logs", "should_raise", "has_config"), [
    (TEST_IMAGE, PUSH_LOGS_1_X, False, False),
    (TEST_IMAGE, PUSH_LOGS_1_9, False, False),
    (TEST_IMAGE, PUSH_LOGS_1_10, False, True),
    (TEST_IMAGE, PUSH_LOGS_1_10_NOT_IN_STATUS, False, False),
    (DOCKER0_REGISTRY + '/' + TEST_IMAGE, PUSH_LOGS_1_X, True, False),
    (DOCKER0_REGISTRY + '/' + TEST_IMAGE, PUSH_LOGS_1_9, True, False),
    (DOCKER0_REGISTRY + '/' + TEST_IMAGE, PUSH_LOGS_1_10, True, True),
    (DOCKER0_REGISTRY + '/' + TEST_IMAGE, PUSH_LOGS_1_10_NOT_IN_STATUS, True, True),
    (TEST_IMAGE, PUSH_ERROR_LOGS, True, False),
])
def test_tag_and_push_plugin(
        tmpdir, monkeypatch, image_name, logs, should_raise, has_config, use_secret):

    if MOCK:
        mock_docker()
        flexmock(docker.Client, push=lambda iid, **kwargs: iter(logs),
                 login=lambda username, registry, dockercfg_path: {'Status': 'Login Succeeded'})

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow({"provider": "git", "uri": "asd"}, TEST_IMAGE)
    workflow.tag_conf.add_primary_image(image_name)
    setattr(workflow, 'builder', X)

    secret_path = None
    if use_secret:
        temp_dir = mkdtemp()
        with open(os.path.join(temp_dir, ".dockercfg"), "w+") as dockerconfig:
            dockerconfig_contents = {
                LOCALHOST_REGISTRY: {
                    "username": "user", "email": "test@example.com", "password": "mypassword"}}
            dockerconfig.write(json.dumps(dockerconfig_contents))
            dockerconfig.flush()
            secret_path = temp_dir

    CONFIG_DIGEST = 'sha256:2c782e3a93d34d89ea4cf54052768be117caed54803263dd1f3798ce42aac14e'
    media_type = 'application/vnd.docker.distribution.manifest.v2+json'

    response_config_json = {
        'config': {
            'digest': CONFIG_DIGEST,
            'mediaType': 'application/octet-stream',
            'size': 4132
        },
        'layers': [
            {
                'digest': 'sha256:16dc1f96e3a1bb628be2e00518fec2bb97bd5933859de592a00e2eb7774b6ecf',
                'mediaType': 'application/vnd.docker.image.rootfs.diff.tar.gzip',
                'size': 71907148
            },
            {
                'digest': 'sha256:cebc0565e1f096016765f55fde87a6f60fdb1208c0b5017e35a856ff578f5ccb',
                'mediaType': 'application/vnd.docker.image.rootfs.diff.tar.gzip',
                'size': 3945724
            }
        ],
        'mediaType': media_type,
        'schemaVersion': 2
    }

    response_json = {
        'config': {
            'Size': 12509448,
            'architecture': 'amd64',
            'author': 'Red Hat, Inc.',
            'config': {
                'Cmd': ['/bin/rsyslog.sh'],
                'Entrypoint': None,
                'Image': 'c3fb36aafd5692d2a45115d32bb120edb6edf6c0c3c783ed6592a8dab969fb88',
                'Labels': {
                    'Architecture': 'x86_64',
                    'Authoritative_Registry': 'registry.access.redhat.com',
                    'BZComponent': 'rsyslog-docker',
                    'Name': 'rhel7/rsyslog',
                    'Release': '28.vrutkovs.31',
                    'Vendor': 'Red Hat, Inc.',
                    'Version': '7.2',
                },
            },
            'created': '2016-10-07T10:20:05.38595Z',
            'docker_version': '1.9.1',
            'id': '1ca220fbc2aed7c141b236c8729fe59db5771b32bf2da74e4a663407f32ec2a2',
            'os': 'linux',
            'parent': '47eed7a8ea3f131e9281ae09fcbfb0041872fd8b74a048f1c739302c8148505d'
        },
        'container_config': {
            'foo': 'bar',
            'spam': 'maps'
        },
        'id': '1ca220fbc2aed7c141b236c8729fe59db5771b32bf2da74e4a663407f32ec2a2',
        'parent_id': 'c3fb36aafd5692d2a45115d32bb120edb6edf6c0c3c783ed6592a8dab969fb88'
    }

    if not has_config:
        response_json = None

    config_latest_url = "https://{}/v2/{}/manifests/latest".format(LOCALHOST_REGISTRY, TEST_IMAGE,)
    config_url = "https://{}/v2/{}/manifests/{}".format(LOCALHOST_REGISTRY, TEST_IMAGE, DIGEST_V2)
    blob_url = "https://{}/v2/{}/blobs/{}".format(
        LOCALHOST_REGISTRY, TEST_IMAGE, CONFIG_DIGEST)

    config_response_config_v1 = requests.Response()
    (flexmock(config_response_config_v1,
              raise_for_status=lambda: None,
              json=response_config_json,
              headers={
                'Content-Type': 'application/vnd.docker.distribution.manifest.v1+json',
                'Docker-Content-Digest': DIGEST_V1
              }
    ))

    config_response_config_v2 = requests.Response()
    (flexmock(config_response_config_v2,
              raise_for_status=lambda: None,
              json=response_config_json,
              headers={
                'Content-Type': 'application/vnd.docker.distribution.manifest.v2+json',
                'Docker-Content-Digest': DIGEST_V2
              }
    ))

    blob_config = requests.Response()
    (flexmock(blob_config, raise_for_status=lambda: None, json=response_json))

    def custom_get(url, headers, **kwargs):
        if url == config_latest_url:
            if headers['Accept'] == 'application/vnd.docker.distribution.manifest.v1+json':
                return config_response_config_v1

            if headers['Accept'] == 'application/vnd.docker.distribution.manifest.v2+json':
                return config_response_config_v2

        if url == config_url:
            return config_response_config_v2

        if url == blob_url:
            return blob_config

    (flexmock(requests)
        .should_receive('get')
        .replace_with(custom_get)
    )

    runner = PostBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': TagAndPushPlugin.key,
            'args': {
                'registries': {
                    LOCALHOST_REGISTRY: {
                        'insecure': True,
                        'secret': secret_path
                    }
                }
            },
        }]
    )

    if should_raise:
        with pytest.raises(Exception):
            runner.run()
    else:
        output = runner.run()
        image = output[TagAndPushPlugin.key][0]
        tasker.remove_image(image)
        assert len(workflow.push_conf.docker_registries) > 0

        if MOCK:
            # we only test this when mocking docker because we don't expect
            # running actual docker against v2 registry
            expected_digest = ManifestDigest(v1=DIGEST_V1, v2=DIGEST_V2)
            assert workflow.push_conf.docker_registries[0].digests[image_name].v1 == expected_digest.v1
            assert workflow.push_conf.docker_registries[0].digests[image_name].v2 == expected_digest.v2

            if has_config:
                assert isinstance(workflow.push_conf.docker_registries[0].config, dict)
            else:
                assert workflow.push_conf.docker_registries[0].config is None
