"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import os
import logging

from dock.core import DockerTasker
from dock.inner import DockerBuildWorkflow
from dock.plugin import PostBuildPluginsRunner
from dock.util import ImageName
from tests.constants import INPUT_IMAGE, SOURCE, LOCALHOST_REGISTRY_HTTP
try:
    import dockpulp
    from dock.plugins.post_push_to_pulp import PulpPushPlugin
except ImportError:
    dockpulp = None

import pytest
from flexmock import flexmock
from tests.docker_mock import mock_docker


class X(object):
    image_id = INPUT_IMAGE
    git_dockerfile_path = None
    git_path = None
    base_image = ImageName(repo="qwe", tag="asd")


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
def test_pulp(tmpdir):
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)
    setattr(workflow, 'tag_conf', X())
    setattr(workflow.tag_conf, 'images', [ImageName(repo="image-name1"),
                                          ImageName(namespace="prefix",
                                                    repo="image-name2")])

    # Mock dockpulp and docker
    dockpulp.Pulp = flexmock(dockpulp.Pulp)
    (flexmock(dockpulp.imgutils).should_receive('get_metadata')
     .with_args(object)
     .and_return([{'id': 'foo'}]))
    (flexmock(dockpulp.imgutils).should_receive('get_versions')
     .with_args(object)
     .and_return({'id': '1.6.0'}))
    flexmock(dockpulp.imgutils).should_receive('check_repo').and_return(0)
    (flexmock(dockpulp.Pulp)
     .should_receive('push_tar_to_pulp')
     .with_args(object, object))
    flexmock(dockpulp.Pulp).should_receive('crane').with_args()
    mock_docker()

    os.environ['SOURCE_SECRET_PATH'] = str(tmpdir)
    with open(os.path.join(str(tmpdir), "pulp.cer"), "wt") as cer:
        cer.write("pulp certificate\n")
    with open(os.path.join(str(tmpdir), "pulp.key"), "wt") as key:
        key.write("pulp key\n")

    runner = PostBuildPluginsRunner(tasker, workflow, [{
        'name': PulpPushPlugin.key,
        'args': {
            'pulp_registry_name': 'test'
        }}])
    runner.run()
    assert PulpPushPlugin.key is not None
