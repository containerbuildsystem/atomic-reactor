"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals
from flexmock import flexmock

import pytest
import os.path

from dockerfile_parse import DockerfileParser
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.post_tag_from_config import TagFromConfigPlugin
from atomic_reactor.util import ImageName
from atomic_reactor.constants import INSPECT_CONFIG
from tests.constants import (MOCK_SOURCE, MOCK)
from tests.fixtures import docker_tasker
if MOCK:
    from tests.docker_mock import mock_docker


class MockSource(object):
    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = os.path.join(tmpdir, 'Dockerfile')
        self.path = tmpdir

    def get_dockerfile_path(self):
        return self.dockerfile_path, self.path


class X(object):
    image_id = "xxx"
    base_image = ImageName.parse("fedora")


def mock_additional_tags_file(tmpdir, tags):
    file_path = os.path.join(tmpdir, 'additional-tags')

    with open(file_path, 'w') as f:
        for tag in tags:
            f.write(tag + '\n')

    return file_path


def mock_workflow(tmpdir):
    if MOCK:
        mock_docker()

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    mock_source = MockSource(tmpdir)
    setattr(workflow, 'builder', X)
    workflow.builder.source = mock_source
    flexmock(workflow, source=mock_source)

    df = DockerfileParser(str(tmpdir))
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    return workflow


@pytest.mark.parametrize(('tags', 'name', 'expected'), [
    ([], 'fedora', []),
    (['spam'], 'fedora', ['fedora:spam']),
    (['spam', 'bacon'], 'foo', ['foo:spam', 'foo:bacon']),
    # ignore tags with hyphens
    (['foo-bar', 'baz'], 'name', ['name:baz']),
    (None, 'fedora', []),
])
def test_tag_from_config_plugin_generated(tmpdir, docker_tasker, tags, name,
                                          expected):
    workflow = mock_workflow(tmpdir)
    workflow.built_image_inspect = {
        INSPECT_CONFIG: {'Labels': {'Name': name}}
    }

    # Simulate missing additional-tags file.
    if tags is not None:
        mock_additional_tags_file(str(tmpdir), tags)

    runner = PostBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{'name': TagFromConfigPlugin.key}]
    )

    results = runner.run()
    plugin_result = results[TagFromConfigPlugin.key]
    assert plugin_result == expected


@pytest.mark.parametrize(('inspect', 'error'), [
    ({'Labels': {}}, "KeyError('Name'"),
    ({}, "KeyError('Labels'"),
    (None, "RuntimeError('There is no inspect data"),
])
def test_bad_inspect_data(tmpdir, docker_tasker, inspect, error):
    workflow = mock_workflow(tmpdir)
    if inspect is not None:
        workflow.built_image_inspect = {
            INSPECT_CONFIG: inspect
        }

    mock_additional_tags_file(str(tmpdir), ['spam', 'bacon'])

    runner = PostBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{'name': TagFromConfigPlugin.key}]
    )

    with pytest.raises(PluginFailedException) as exc:
        runner.run()

    assert error in str(exc)
