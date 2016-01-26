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
from tests.constants import MOCK, MOCK_SOURCE, LOCALHOST_REGISTRY

if MOCK:
    from tests.docker_mock import mock_docker

class MockSource(object):
    dockerfile_path = None
    path = None

class MockBuilder(object):
    image_id = "xxx"
    source = MockSource()
    base_image = None

BASE_IMAGE = "busybox:latest"
BASE_IMAGE_W_LIBRARY = "library/" + BASE_IMAGE
BASE_IMAGE_W_REGISTRY = LOCALHOST_REGISTRY + "/" + BASE_IMAGE
BASE_IMAGE_W_LIB_REG = LOCALHOST_REGISTRY + "/" + BASE_IMAGE_W_LIBRARY
@pytest.mark.parametrize(('parent_registry',
                          'df_base',     # the base image is always expected
                          'expected',    # additional expected images
                          'not_expected' # additional images not expected
), [
    #expected_w_reg,expected_w_lib_reg,expected_wo_reg', [
    (LOCALHOST_REGISTRY, BASE_IMAGE,
     # expected:
     [BASE_IMAGE_W_REGISTRY],
     # not expected:
     [BASE_IMAGE_W_LIB_REG]),

    (LOCALHOST_REGISTRY, BASE_IMAGE_W_REGISTRY,
     # expected:
     [BASE_IMAGE_W_REGISTRY],
     # not expected:
     [BASE_IMAGE_W_LIB_REG]),

    (None, BASE_IMAGE,
     # expected:
     [],
     # not expected:
     [BASE_IMAGE_W_REGISTRY, BASE_IMAGE_W_LIB_REG]),

    (None, BASE_IMAGE_W_REGISTRY,
     # expected:
     [BASE_IMAGE_W_REGISTRY],
     # not expected:
     [BASE_IMAGE_W_LIB_REG]),

    # Tests with explicit "library" namespace:

    (LOCALHOST_REGISTRY, BASE_IMAGE_W_LIB_REG,
     # expected:
     [],
     # not expected:
     [BASE_IMAGE_W_REGISTRY]),

    (None, BASE_IMAGE_W_LIB_REG,
     # expected:
     [],
     # not expected:
     [BASE_IMAGE_W_REGISTRY]),

    # For this test, ensure 'library-only' is only available through
    # the 'library' namespace. docker_mock takes care of this when
    # mocking.
    (LOCALHOST_REGISTRY, "library-only:latest",
     # expected:
     [LOCALHOST_REGISTRY + "/library/library-only:latest"],
     [LOCALHOST_REGISTRY + "/library-only:latest"]),
])
def test_pull_base_image_plugin(parent_registry, df_base, expected, not_expected):
    if MOCK:
        mock_docker(remember_images=True)

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.builder = MockBuilder()
    workflow.builder.base_image = ImageName.parse(df_base)

    expected = set(expected)
    expected.add(df_base)
    all_images = set(expected).union(not_expected)
    for image in all_images:
        assert not tasker.image_exists(image)

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': PullBaseImagePlugin.key,
            'args': {'parent_registry': parent_registry, 'parent_registry_insecure': True}
        }]
    )

    runner.run()

    for image in expected:
        assert tasker.image_exists(image)

    for image in not_expected:
        assert not tasker.image_exists(image)

    for image in all_images:
        try:
            tasker.remove_image(image)
        except:
            pass


def test_pull_base_wrong_registry():
    with pytest.raises(PluginFailedException):
        test_pull_base_image_plugin('localhost:1234', BASE_IMAGE_W_REGISTRY, [], [])
