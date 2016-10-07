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
@pytest.mark.parametrize(("image_name", "logs", "should_raise"), [
    (TEST_IMAGE, PUSH_LOGS_1_X, False),
    (TEST_IMAGE, PUSH_LOGS_1_9, False),
    (TEST_IMAGE, PUSH_LOGS_1_10, False),
    (TEST_IMAGE, PUSH_LOGS_1_10_NOT_IN_STATUS, False),
    (DOCKER0_REGISTRY + '/' + TEST_IMAGE, PUSH_LOGS_1_X, True),
    (DOCKER0_REGISTRY + '/' + TEST_IMAGE, PUSH_LOGS_1_9, True),
    (DOCKER0_REGISTRY + '/' + TEST_IMAGE, PUSH_LOGS_1_10, True),
    (DOCKER0_REGISTRY + '/' + TEST_IMAGE, PUSH_LOGS_1_10_NOT_IN_STATUS, True),
    (TEST_IMAGE, PUSH_ERROR_LOGS, True),
])
def test_tag_and_push_plugin(tmpdir, monkeypatch, image_name, logs, should_raise, use_secret):

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

    (flexmock(atomic_reactor.util)
        .should_receive('get_manifest_digests')
        .and_return(ManifestDigest(v1=DIGEST_V1, v2=DIGEST_V2))
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
            assert workflow.push_conf.docker_registries[0].digests[image_name].v1 == DIGEST_V1
            assert workflow.push_conf.docker_registries[0].digests[image_name].v2 == DIGEST_V2
