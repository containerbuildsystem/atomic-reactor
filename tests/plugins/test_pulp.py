"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import os
import logging

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner
from atomic_reactor.util import ImageName
from tests.constants import INPUT_IMAGE, SOURCE, LOCALHOST_REGISTRY_HTTP
try:
    import dockpulp
    from atomic_reactor.plugins.post_push_to_pulp import PulpPushPlugin
except (ImportError, SyntaxError):
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
                                                    repo="image-name2"),
                                          ImageName(repo="image-name3", tag="asd")])

    # Mock dockpulp and docker
    dockpulp.Pulp = flexmock(dockpulp.Pulp)
    dockpulp.Pulp.registry='registry.example.com'
    (flexmock(dockpulp.imgutils).should_receive('get_metadata')
     .with_args(object)
     .and_return([{'id': 'foo'}]))
    (flexmock(dockpulp.imgutils).should_receive('get_versions')
     .with_args(object)
     .and_return({'id': '1.6.0'}))
    (flexmock(dockpulp.imgutils).should_receive('check_repo')
     .and_return(3)
     .and_return(2)
     .and_return(1)
     .and_return(0))
    (flexmock(dockpulp.Pulp)
     .should_receive('push_tar_to_pulp')
     .with_args(object, object))
    (flexmock(dockpulp.Pulp)
     .should_receive('crane')
     .with_args(repos=list))
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
    with pytest.raises(Exception) as rc3:
        runner.run()
    with pytest.raises(Exception) as rc2:
        runner.run()
    with pytest.raises(Exception) as rc1:
        runner.run()
    runner.run()
    assert PulpPushPlugin.key is not None
    images = [i.to_str() for i in workflow.postbuild_results[PulpPushPlugin.key]]
    assert "registry.example.com/image-name1" in images
    assert "registry.example.com/prefix/image-name2" in images
    assert "registry.example.com/image-name3:asd" in images
