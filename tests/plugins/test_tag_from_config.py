"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from flexmock import flexmock
from textwrap import dedent

import pytest
import os.path

from atomic_reactor.inner import BuildResult
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.post_tag_from_config import TagFromConfigPlugin
from atomic_reactor.util import df_parser
from atomic_reactor.constants import INSPECT_CONFIG
from tests.constants import IMPORTED_IMAGE_ID


DF_CONTENT_LABELS = '''\
FROM fedora
LABEL "name"="name_value"
LABEL "version"="version_value"
LABEL "release"="$parentrelease"
'''

TEST_VERSION = "v1.2.5"
TEST_IMAGE = f"holy-hand-grenade:{TEST_VERSION}"

pytestmark = pytest.mark.usefixtures('user_params')


class MockSource(object):
    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = os.path.join(tmpdir, 'Dockerfile')
        self.path = tmpdir

    def get_build_file_path(self):
        return self.dockerfile_path, self.path


@pytest.fixture
def workflow(workflow, source_dir):
    mock_source = MockSource(source_dir)
    flexmock(workflow, source=mock_source)

    df = df_parser(str(source_dir))
    flexmock(workflow, df_path=df.dockerfile_path)
    workflow.df_dir = str(source_dir)

    return workflow


@pytest.mark.parametrize(('inspect', 'error'), [  # noqa
    ({'Labels': {}}, "KeyError: <object"),
    ({}, "KeyError: 'Labels'"),
    (None, "RuntimeError: There is no inspect data"),
])
def test_bad_inspect_data(workflow, inspect, error):
    if inspect is not None:
        workflow.built_image_inspect = {
            INSPECT_CONFIG: inspect
        }
    workflow.build_result = BuildResult(image_id=IMPORTED_IMAGE_ID)

    runner = PostBuildPluginsRunner(
        workflow,
        [{'name': TagFromConfigPlugin.key}]
    )

    with pytest.raises(PluginFailedException) as exc:
        runner.run()

    assert error in str(exc.value)


@pytest.mark.parametrize(('floating_tags', 'unique_tags', 'primary_tags', 'expected'), [  # noqa
    ([], [], [], []),
    ([], ['foo', 'bar'], [], ['name_value:foo', 'name_value:bar']),
    ([], [], ['foo', 'bar'], ['name_value:foo', 'name_value:bar']),
    ([], [], ['foo', '{unknown}', 'bar'], None),
    ([], [], ['foo', '{version}', 'bar'],
     ['name_value:foo', 'name_value:version_value', 'name_value:bar']),
    ([], [], ['foo', '{version}-{release}', 'bar'],
     ['name_value:foo', 'name_value:version_value-7.4.1', 'name_value:bar']),
    ([], ['foo', 'bar'], ['{version}'],
     ['name_value:foo', 'name_value:bar', 'name_value:version_value']),
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
def test_tag_parse(workflow, floating_tags, unique_tags, primary_tags, expected):
    df = df_parser(workflow.source.path)
    df.content = DF_CONTENT_LABELS

    flexmock(workflow, df_path=df.dockerfile_path)
    workflow.build_result = BuildResult.make_remote_image_result()

    base_inspect = {
        INSPECT_CONFIG: {
            'Labels': {'parentrelease': '7.4.1'},
            'Env': {'parentrelease': '7.4.1'},
        }
    }
    flexmock(workflow.imageutil).should_receive('base_image_inspect').and_return(base_inspect)

    if unique_tags is not None and primary_tags is not None and floating_tags is not None:
        input_tags = {
            'unique': unique_tags,
            'primary': primary_tags,
            'floating': floating_tags,
        }
    else:
        input_tags = None

    plugin = TagFromConfigPlugin(workflow)
    plugin.tag_suffixes = input_tags

    if expected is not None:
        plugin_result = plugin.run()

        # Plugin should return the tags we expect
        assert plugin_result == expected

        # Workflow should have the expected tags configured
        for tag in expected:
            assert any(tag == str(image) for image in workflow.tag_conf.images)

        # Workflow should not have any other tags configured
        assert len(workflow.tag_conf.images) == len(expected)
    else:
        with pytest.raises(KeyError):
            plugin.run()


@pytest.mark.parametrize(('name', 'organization', 'expected'), (
    ('etcd', None, 'etcd'),
    ('etcd', 'org', 'org/etcd'),
    ('custom/etcd', None, 'custom/etcd'),
    ('custom/etcd', 'org', 'org/custom-etcd'),
))
def test_tags_enclosed(workflow, name, organization, expected):
    df = df_parser(workflow.source.path)
    df.content = dedent("""\
        FROM fedora
        LABEL "name"="{}"
        LABEL "version"="1.7"
        LABEL "release"="99"
    """.format(name))

    workflow.build_result = BuildResult.make_remote_image_result()

    if organization:
        reactor_config = {
            'version': 1,
            'registries_organization': organization
        }
        workflow.conf.conf = reactor_config

    input_tags = {
        'unique': ['foo', 'bar'],
        'primary': ['{version}', '{version}-{release}'],
    }

    plugin = TagFromConfigPlugin(workflow)
    plugin.tag_suffixes = input_tags

    plugin_result = plugin.run()

    expected_tags = ['{}:{}'.format(expected, tag) for tag in ['foo', 'bar', '1.7', '1.7-99']]
    # Plugin should return the tags we expect
    assert plugin_result == expected_tags

    # Workflow should have the expected tags configured
    for tag in expected_tags:
        assert any(tag == str(image) for image in workflow.tag_conf.images)

    # Workflow should not have any other tags configured
    assert len(workflow.tag_conf.images) == len(expected_tags)


@pytest.mark.parametrize(
    "user_params, is_orchestrator, expect_suffixes",
    [
        # default worker tags
        (
            {"image_tag": TEST_IMAGE},
            False,
            {"unique": [TEST_VERSION], "primary": [], "floating": []},
        ),
        # default orchestrator tags
        (
            {"image_tag": TEST_IMAGE},
            True,
            {
                "unique": [TEST_VERSION],
                "primary": ["{version}-{release}"],
                "floating": ["latest", "{version}"],
            },
        ),
        # scratch build
        (
            {"image_tag": TEST_IMAGE, "scratch": True},
            True,
            {
                "unique": [TEST_VERSION],
                "primary": [],
                "floating": [],
            },
        ),
        # isolated build
        (
            {"image_tag": TEST_IMAGE, "isolated": True},
            True,
            {
                "unique": [TEST_VERSION],
                "primary": ["{version}-{release}"],
                "floating": [],
            },
        ),
        # additional tags from additional-tags file
        (
            {"image_tag": TEST_IMAGE, "additional_tags": ["spam"]},
            True,
            {
                "unique": [TEST_VERSION],
                "primary": ["{version}-{release}"],
                "floating": ["latest", "{version}", "spam"],
            },
        ),
        # additional tags from container.yaml
        (
            {"image_tag": TEST_IMAGE, "additional_tags": ["spam"], "tags_from_yaml": True},
            True,
            {
                "unique": [TEST_VERSION],
                "primary": ["{version}-{release}"],
                "floating": ["spam"],
            },
        ),
        # additional tags don't apply if build is scratch
        (
            {"image_tag": TEST_IMAGE, "additional_tags": ["spam"], "scratch": True},
            True,
            {
                "unique": [TEST_VERSION],
                "primary": [],
                "floating": [],
            },
        ),
        # additional tags don't apply if build is isolated
        (
            {"image_tag": TEST_IMAGE, "additional_tags": ["spam"], "isolated": True},
            True,
            {
                "unique": [TEST_VERSION],
                "primary": ["{version}-{release}"],
                "floating": [],
            },
        ),
    ],
)
def test_tag_suffixes_from_user_params(user_params, is_orchestrator, expect_suffixes, workflow):
    workflow.user_params.update(user_params)

    plugin = TagFromConfigPlugin(workflow)
    flexmock(plugin).should_receive("is_in_orchestrator").and_return(is_orchestrator)

    assert plugin.tag_suffixes == expect_suffixes
