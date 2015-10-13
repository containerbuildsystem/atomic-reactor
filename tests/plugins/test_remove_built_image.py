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
from atomic_reactor.plugins.exit_remove_built_image import GarbageCollectionPlugin
from atomic_reactor.plugins.post_tag_and_push import TagAndPushPlugin
from atomic_reactor.util import ImageName
from tests.constants import LOCALHOST_REGISTRY, TEST_IMAGE, INPUT_IMAGE, MOCK

if MOCK:
    from tests.docker_mock import mock_docker


class Y(object):
    pass


class X(object):
    image_id = INPUT_IMAGE
    source = Y()
    source.dockerfile_path = None
    source.path = None
    base_image = ImageName(repo="qwe", tag="asd")


def test_remove_built_image_plugin(tmpdir):
    if MOCK:
        mock_docker()

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow({"provider": "git", "uri": "asd"},
                                   TEST_IMAGE)
    workflow.postbuild_results[TagAndPushPlugin.key] = True
    workflow.tag_conf.add_primary_image(TEST_IMAGE)
    workflow.push_conf.add_docker_registry(LOCALHOST_REGISTRY, insecure=True)
    setattr(workflow, 'builder', X)

    runner = PostBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': GarbageCollectionPlugin.key,
        }]
    )
    output = runner.run()
