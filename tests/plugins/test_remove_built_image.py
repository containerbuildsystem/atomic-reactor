"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals, absolute_import

import flexmock
import pytest

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner
from atomic_reactor.plugins.exit_remove_built_image import (GarbageCollectionPlugin,
                                                            defer_removal)
from atomic_reactor.plugins.post_tag_and_push import TagAndPushPlugin
from osbs.utils import ImageName
from tests.constants import (LOCALHOST_REGISTRY,
                             TEST_IMAGE,
                             IMPORTED_IMAGE_ID,
                             INPUT_IMAGE,
                             MOCK, MOCK_SOURCE)

if MOCK:
    from tests.docker_mock import mock_docker


class X(object):
    pass


def mock_environment(base_image=None):
    if MOCK:
        mock_docker()

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(source=MOCK_SOURCE)
    workflow.postbuild_results[TagAndPushPlugin.key] = True
    workflow.tag_conf.add_primary_image(TEST_IMAGE)
    workflow.push_conf.add_docker_registry(LOCALHOST_REGISTRY, insecure=True)
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'image_id', INPUT_IMAGE)
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)
    base_image = ImageName.parse(IMPORTED_IMAGE_ID)
    setattr(workflow.builder, 'base_image', base_image)
    workflow.pulled_base_images.add(IMPORTED_IMAGE_ID)
    return tasker, workflow


@pytest.mark.usefixtures('user_params')
class TestGarbageCollectionPlugin(object):
    @pytest.mark.parametrize(('remove_base', 'deferred', 'expected'), [
        (False, [], {INPUT_IMAGE}),
        (False, ['defer'], {INPUT_IMAGE, 'defer'}),
        (True, [], {IMPORTED_IMAGE_ID, INPUT_IMAGE}),
        (True, ['defer'], {IMPORTED_IMAGE_ID, INPUT_IMAGE, 'defer'}),
    ])
    def test_remove_built_image_plugin(self, remove_base, deferred, expected):
        tasker, workflow = mock_environment()
        runner = PostBuildPluginsRunner(
            tasker,
            workflow,
            [{
                'name': GarbageCollectionPlugin.key,
                'args': {'remove_pulled_base_image': remove_base},
            }]
        )
        removed_images = []

        def spy_remove_image(image_id, force=None):
            removed_images.append(image_id)

        flexmock(tasker, remove_image=spy_remove_image)
        for image in deferred:
            defer_removal(workflow, image)

        runner.run()
        image_set = set(removed_images)
        assert len(image_set) == len(removed_images)
        assert image_set == expected
