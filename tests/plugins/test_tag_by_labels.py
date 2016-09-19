"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner
from atomic_reactor.plugins.post_tag_and_push import TagAndPushPlugin
from atomic_reactor.plugins.post_tag_by_labels import TagByLabelsPlugin
from atomic_reactor.util import ImageName
from atomic_reactor.constants import INSPECT_CONFIG
from tests.constants import LOCALHOST_REGISTRY, TEST_IMAGE, INPUT_IMAGE, MOCK

if MOCK:
    from tests.docker_mock import mock_docker

import pytest


class Y(object):
    pass


class X(object):
    image_id = INPUT_IMAGE
    source = Y()
    source.dockerfile_path = None
    source.path = None
    base_image = ImageName(repo="qwe", tag="asd")
    image = ImageName.parse("test-image:unique_tag_123")


@pytest.mark.parametrize('args', [
    {},
    {'unique_tag_only': True},
])
def test_tag_by_labels_plugin(tmpdir, args):
    if MOCK:
        mock_docker()

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow({"provider": "git", "uri": "asd"}, "test-image")
    version = "1.0"
    release = "1"
    workflow.built_image_inspect = {
        INSPECT_CONFIG: {
            "Labels": {
                "name": TEST_IMAGE,
                "version": version,
                "release": release
            }
        }
    }
    workflow.push_conf.add_docker_registry(LOCALHOST_REGISTRY, insecure=True)
    image = ImageName(repo=TEST_IMAGE,
                      tag="%s_%s" % (version, release),
                      registry=LOCALHOST_REGISTRY)

    setattr(workflow, 'builder', X)

    runner = PostBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': TagByLabelsPlugin.key,
            'args': args,
        }]
    )
    output = runner.run()
    assert TagByLabelsPlugin.key in output.keys()

    images = [i.to_str() for i in workflow.tag_conf.images]
    primary_images = [i.to_str() for i in workflow.tag_conf.primary_images]
    unique_images = [i.to_str() for i in workflow.tag_conf.unique_images]

    if args.get('unique_tag_only'):
        assert len(workflow.tag_conf.images) == 1
        assert len(primary_images) == 0

    else:
        assert len(workflow.tag_conf.images) == 4
        assert len(primary_images) == 3

        assert ("%s:%s-%s" % (TEST_IMAGE, version, release)) in images
        assert ("%s:%s" % (TEST_IMAGE, version)) in images
        assert ("%s:latest" % (TEST_IMAGE, )) in images

        assert ("%s:%s-%s" % (TEST_IMAGE, version, release)) in primary_images
        assert ("%s:%s" % (TEST_IMAGE, version)) in primary_images
        assert ("%s:latest" % (TEST_IMAGE, )) in primary_images

    assert len(unique_images) == 1
    assert ("%s:%s" % (TEST_IMAGE, "unique_tag_123")) in images
    assert ("%s:%s" % (TEST_IMAGE, "unique_tag_123")) in unique_images
    tasker.remove_image(image)
