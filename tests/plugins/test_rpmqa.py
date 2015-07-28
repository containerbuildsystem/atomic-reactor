"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import json
import os

import docker
from flexmock import flexmock
import pytest

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin
from atomic_reactor.util import ImageName
from tests.constants import DOCKERFILE_GIT
from tests.docker_mock import mock_docker


TEST_IMAGE = "fedora:latest"
SOURCE = {"provider": "git", "uri": DOCKERFILE_GIT}


class X(object):
    pass


PACKAGE_LIST = [b'package1', b'package2']

def mock_logs(cid, **kwargs):
    return b"\n".join(PACKAGE_LIST)


def test_rpmqa_plugin():
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'base_image', ImageName(repo='fedora', tag='21'))
    setattr(workflow.builder, "source", X())
    setattr(workflow.builder.source, 'dockerfile_path', "/non/existent")
    setattr(workflow.builder.source, 'path', "/non/existent")
    mock_docker()
    flexmock(docker.Client, logs=mock_logs)
    runner = PostBuildPluginsRunner(tasker, workflow,
                                    [{"name": PostBuildRPMqaPlugin.key,
                                      "args": {'image_id': TEST_IMAGE}}])
    results = runner.run()
    assert results is not None
    assert results[PostBuildRPMqaPlugin.key] == ['package1', 'package2']


def mock_logs_raise(cid, **kwargs):
    raise RuntimeError


def test_rpmqa_plugin_exception():
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'base_image', ImageName(repo='fedora', tag='21'))
    setattr(workflow.builder, "source", X())
    setattr(workflow.builder.source, 'dockerfile_path', "/non/existent")
    setattr(workflow.builder.source, 'path', "/non/existent")
    mock_docker()
    flexmock(docker.Client, logs=mock_logs_raise)
    runner = PostBuildPluginsRunner(tasker, workflow,
                                    [{"name": PostBuildRPMqaPlugin.key,
                                      "args": {'image_id': TEST_IMAGE}}])
    results = runner.run()

    assert results is not None
    assert results[PostBuildRPMqaPlugin.key] is not None
    assert isinstance(results[PostBuildRPMqaPlugin.key], Exception)
