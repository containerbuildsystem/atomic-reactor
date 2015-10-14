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
from atomic_reactor.util import ImageName
from tests.constants import LOCALHOST_REGISTRY, TEST_IMAGE, INPUT_IMAGE, MOCK, DOCKER0_REGISTRY

import json

if MOCK:
    import docker
    from flexmock import flexmock
    from tests.docker_mock import mock_docker

DIGEST1 = 'sha256:28b64a8b29fd2723703bb17acf907cd66898440270e536992b937899a4647414'
PUSH_LOGS = [
    b'{"status":"The push refers to a repository [172.17.42.1:5000/ns/test-image2] (len: 1)"}',
    b'{"status":"Buffering to Disk","progressDetail":{},"id":"83bca0dcfd1b"}',
    b'{"status":"Pushing","progressDetail":{"current":1,"total":32},"progress":"[=\\u003e                                                 ]      1 B/32 B","id":"83bca0dcfd1b"}',
    b'{"status":"Pushing","progressDetail":{"current":66813953,"total":66944370},"progress":"[=================================================\\u003e ] 66.81 MB/66.94 MB","id":"ded7cd95e059"}',
    b'{"status":"Pushing","progressDetail":{"current":66944370,"total":66944370},"progress":"[==================================================\\u003e] 66.94 MB/66.94 MB","id":"ded7cd95e059"}',
    b'{"status":"Image successfully pushed","progressDetail":{},"id":"ded7cd95e059"}',
    b'{"status":"Image already exists","progressDetail":{},"id":"48ecf305d2cf"}',
    b'{"status":"Digest: ' + DIGEST1.encode('utf-8') + b'"}']


class Y(object):
    pass


class X(object):
    image_id = INPUT_IMAGE
    source = Y()
    source.dockerfile_path = None
    source.path = None
    base_image = ImageName(repo="qwe", tag="asd")

@pytest.mark.parametrize(("image_name", "should_raise"), [
    (TEST_IMAGE, False),
    (DOCKER0_REGISTRY + '/' + TEST_IMAGE, True),
])
def test_tag_and_push_plugin(tmpdir, image_name, should_raise):
    if MOCK:
        mock_docker()
        flexmock(docker.Client, push=lambda iid, **kwargs: PUSH_LOGS)

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow({"provider": "git", "uri": "asd"}, TEST_IMAGE)
    workflow.tag_conf.add_primary_image(image_name)
    setattr(workflow, 'builder', X)

    runner = PostBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': TagAndPushPlugin.key,
            'args': {
                'registries': {
                    LOCALHOST_REGISTRY: {
                        'insecure': True
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
            assert workflow.push_conf.docker_registries[0].digests[image_name] == DIGEST1

def test_extract_digest():
    json_logs = [json.loads(l.decode('utf-8')) for l in PUSH_LOGS]
    digest = TagAndPushPlugin.extract_digest(json_logs)
    assert digest == DIGEST1
