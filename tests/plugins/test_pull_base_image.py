"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import pytest

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.util import ImageName
from atomic_reactor.plugins.pre_pull_base_image import PullBaseImagePlugin
from tests.constants import MOCK, MOCK_SOURCE, BASE_IMAGE, BASE_IMAGE_W_REGISTRY, \
                            LOCALHOST_REGISTRY

if MOCK:
    from tests.docker_mock import mock_docker

class MockSource(object):
    dockerfile_path = None
    path = None

class MockBuilder(object):
    image_id = "xxx"
    source = MockSource()
    base_image = None

@pytest.mark.parametrize('df_base,parent_registry,expected_w_reg,expected_wo_reg', [
    (BASE_IMAGE,            LOCALHOST_REGISTRY, True, True),
    (BASE_IMAGE_W_REGISTRY, LOCALHOST_REGISTRY, True, False),
    (BASE_IMAGE,            None,               False, True),
    (BASE_IMAGE_W_REGISTRY, None,               True, False),
])
def test_pull_base_image_plugin(df_base, parent_registry, expected_w_reg, expected_wo_reg):
    if MOCK:
        mock_docker(remember_images=True)

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.parent_registry = parent_registry
    workflow.builder = MockBuilder()
    workflow.builder.base_image = ImageName.parse(df_base)

    assert not tasker.image_exists(BASE_IMAGE)
    assert not tasker.image_exists(BASE_IMAGE_W_REGISTRY)

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': PullBaseImagePlugin.key
        }]
    )

    runner.run()

    assert tasker.image_exists(BASE_IMAGE_W_REGISTRY) == expected_w_reg
    assert tasker.image_exists(BASE_IMAGE) == expected_wo_reg

def test_pull_base_wrong_registry():
    with pytest.raises(PluginFailedException):
        test_pull_base_image_plugin(BASE_IMAGE_W_REGISTRY, 'localhost:1234', True, False)
