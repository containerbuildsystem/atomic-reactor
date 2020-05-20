"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import
from flexmock import flexmock
from textwrap import dedent

import pytest
import os.path

from atomic_reactor.build import BuildResult
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.post_tag_from_config import TagFromConfigPlugin
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin, ReactorConfig,
                                                       WORKSPACE_CONF_KEY)
from atomic_reactor.util import df_parser
from osbs.utils import ImageName
from atomic_reactor.constants import INSPECT_CONFIG
from tests.constants import (MOCK_SOURCE, MOCK, IMPORTED_IMAGE_ID)
if MOCK:
    from tests.docker_mock import mock_docker


DF_CONTENT_LABELS = '''\
FROM fedora
LABEL "name"="name_value"
LABEL "version"="version_value"
LABEL "release"="$parentrelease"
'''


class MockSource(object):
    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = os.path.join(tmpdir, 'Dockerfile')
        self.path = tmpdir

    def get_build_file_path(self):
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

    workflow = DockerBuildWorkflow('test-image', source=MOCK_SOURCE)
    mock_source = MockSource(tmpdir)
    setattr(workflow, 'builder', X)
    workflow.builder.source = mock_source
    flexmock(workflow, source=mock_source)

    df = df_parser(str(tmpdir))
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    return workflow


@pytest.mark.parametrize(('tags', 'name', 'expected'), [  # noqa
    ([], 'fedora', []),
    (['spam'], 'fedora', ['fedora:spam']),
    (['spam', 'bacon'], 'foo', ['foo:spam', 'foo:bacon']),
    # ignore tags with hyphens
    (['foo-bar', 'baz'], 'name', ['name:baz']),
    # make sure that tags are also valid
    (['illegal@char', '.starts.with.dot'], 'bar', []),
    (['has_under', 'ends.dot.'], 'bar', ['bar:has_under', 'bar:ends.dot.']),
    (None, 'fedora', []),
])
def test_tag_from_config_plugin_generated(tmpdir, docker_tasker, tags, name,
                                          expected):
    workflow = mock_workflow(tmpdir)
    workflow.built_image_inspect = {
        INSPECT_CONFIG: {'Labels': {'Name': name}}
    }
    workflow.build_result = BuildResult(image_id=IMPORTED_IMAGE_ID)

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


@pytest.mark.parametrize(('inspect', 'error'), [  # noqa
    ({'Labels': {}}, "KeyError: <object"),
    ({}, "KeyError: 'Labels'"),
    (None, "RuntimeError: There is no inspect data"),
])
def test_bad_inspect_data(tmpdir, docker_tasker, inspect, error):
    workflow = mock_workflow(tmpdir)
    if inspect is not None:
        workflow.built_image_inspect = {
            INSPECT_CONFIG: inspect
        }
    workflow.build_result = BuildResult(image_id=IMPORTED_IMAGE_ID)

    mock_additional_tags_file(str(tmpdir), ['spam', 'bacon'])

    runner = PostBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{'name': TagFromConfigPlugin.key}]
    )

    with pytest.raises(PluginFailedException) as exc:
        runner.run()

    assert error in str(exc.value)


@pytest.mark.parametrize(('floating_tags', 'unique_tags', 'primary_tags', 'expected'), [  # noqa
    (None, None, None, ['name_value:get_tags', 'name_value:file_tags']),
    ([], [], [], []),
    ([], ['foo', 'bar'], [], ['name_value:foo', 'name_value:bar']),
    ([], [], ['foo', 'bar'], ['name_value:foo', 'name_value:bar']),
    ([], [], ['foo', '{unknown}', 'bar'], None),
    ([], [], ['foo', '{version}', 'bar'], ['name_value:foo', 'name_value:version_value',
                                       'name_value:bar']),
    ([], [], ['foo', '{version}-{release}', 'bar'],
     ['name_value:foo', 'name_value:version_value-7.4.1', 'name_value:bar']),
    ([], ['foo', 'bar'], ['{version}'], ['name_value:foo', 'name_value:bar',
                                     'name_value:version_value']),
    (['bar'], ['foo'], ['{version}'], ['name_value:foo', 'name_value:bar',
                                       'name_value:version_value']),
    ([], ['foo', 'bar'], ['{version}-{release}'],
     ['name_value:foo', 'name_value:bar', 'name_value:version_value-7.4.1']),
    (['bar'], ['foo'], ['{version}-{release}'], ['name_value:foo', 'name_value:bar',
                                                 'name_value:version_value-7.4.1']),
    ([], ['foo', 'bar'], ['baz', '{version}', 'version_value', 'baz'],
     ['name_value:foo', 'name_value:bar', 'name_value:baz',
      'name_value:version_value']),
    (['bar'], ['foo'], ['baz', '{version}', 'version_value', 'baz'],
     ['name_value:foo', 'name_value:bar', 'name_value:baz',
      'name_value:version_value']),
])
def test_tag_parse(tmpdir, docker_tasker, floating_tags, unique_tags, primary_tags, expected):
    df = df_parser(str(tmpdir))
    df.content = DF_CONTENT_LABELS

    workflow = mock_workflow(tmpdir)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)
    workflow.build_result = BuildResult.make_remote_image_result()

    base_inspect = {
        INSPECT_CONFIG: {
            'Labels': {'parentrelease': '7.4.1'},
            'Env': {'parentrelease': '7.4.1'},
        }
    }
    setattr(workflow.builder, 'base_image_inspect', base_inspect)
    mock_additional_tags_file(str(tmpdir), ['get_tags', 'file_tags'])

    if unique_tags is not None and primary_tags is not None and floating_tags is not None:
        input_tags = {
            'unique': unique_tags,
            'primary': primary_tags,
            'floating': floating_tags,
        }
    else:
        input_tags = None
    runner = PostBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{'name': TagFromConfigPlugin.key,
          'args': {'tag_suffixes': input_tags}}]
    )
    if expected is not None:
        results = runner.run()
        plugin_result = results[TagFromConfigPlugin.key]

        # Plugin should return the tags we expect
        assert plugin_result == expected

        # Workflow should have the expected tags configured
        for tag in expected:
            assert any(tag == str(image) for image in workflow.tag_conf.images)

        # Workflow should not have any other tags configured
        assert len(workflow.tag_conf.images) == len(expected)
    else:
        with pytest.raises(PluginFailedException):
            runner.run()


@pytest.mark.parametrize(('name', 'organization', 'expected'), (
    ('etcd', None, 'etcd'),
    ('etcd', 'org', 'org/etcd'),
    ('custom/etcd', None, 'custom/etcd'),
    ('custom/etcd', 'org', 'org/custom-etcd'),
))
def test_tags_enclosed(tmpdir, docker_tasker, name, organization, expected):
    df = df_parser(str(tmpdir))
    df.content = dedent("""\
        FROM fedora
        LABEL "name"="{}"
        LABEL "version"="1.7"
        LABEL "release"="99"
    """.format(name))

    workflow = mock_workflow(tmpdir)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)
    workflow.build_result = BuildResult.make_remote_image_result()

    if organization:
        reactor_config = ReactorConfig({
            'version': 1,
            'registries_organization': organization
        })
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {WORKSPACE_CONF_KEY: reactor_config}

    input_tags = {
        'unique': ['foo', 'bar'],
        'primary': ['{version}', '{version}-{release}'],
    }

    runner = PostBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{'name': TagFromConfigPlugin.key,
          'args': {'tag_suffixes': input_tags}}]
    )

    results = runner.run()
    plugin_result = results[TagFromConfigPlugin.key]

    expected_tags = ['{}:{}'.format(expected, tag) for tag in ['foo', 'bar', '1.7', '1.7-99']]
    # Plugin should return the tags we expect
    assert plugin_result == expected_tags

    # Workflow should have the expected tags configured
    for tag in expected_tags:
        assert any(tag == str(image) for image in workflow.tag_conf.images)

    # Workflow should not have any other tags configured
    assert len(workflow.tag_conf.images) == len(expected_tags)
