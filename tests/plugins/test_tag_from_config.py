"""
Copyright (c) 2016-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from flexmock import flexmock
from textwrap import dedent

import pytest

from atomic_reactor.plugins.pre_tag_from_config import TagFromConfigPlugin
from atomic_reactor.constants import INSPECT_CONFIG


DF_CONTENT_LABELS = '''\
FROM fedora
LABEL "name"="name_value"
LABEL "version"="version_value"
LABEL "release"="$parentrelease"
'''

TEST_VERSION = "v1.2.5"
TEST_IMAGE = f"holy-hand-grenade:{TEST_VERSION}"
REGISTRY = 'registry.com'

pytestmark = pytest.mark.usefixtures('user_params')


@pytest.fixture
def workflow(workflow, source_dir):
    flexmock(workflow.conf, registry={'uri': REGISTRY})

    workflow.build_dir.init_build_dirs(["x86_64"], workflow.source)
    workflow.build_dir.any_platform.dockerfile.content = DF_CONTENT_LABELS

    return workflow


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
def test_tag_from_config(workflow, floating_tags, unique_tags, primary_tags, expected):
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
        plugin.run()

        # Testing the whole image pullspecs with the registry
        expected_images = [f"{REGISTRY}/{image}" for image in expected]
        actual_images = [str(image) for image in workflow.data.tag_conf.images]
        assert sorted(actual_images) == sorted(expected_images)
    else:
        with pytest.raises(KeyError):
            plugin.run()


@pytest.mark.parametrize(('name', 'organization', 'expected'), (
    ('etcd', None, 'etcd'),
    ('etcd', 'org', 'org/etcd'),
    ('custom/etcd', None, 'custom/etcd'),
    ('custom/etcd', 'org', 'org/custom-etcd'),
))
def test_tag_from_config_with_tags_enclosed(workflow, name, organization, expected):
    df_content = dedent("""\
        FROM fedora
        LABEL "name"="{}"
        LABEL "version"="1.7"
        LABEL "release"="99"
    """.format(name))

    workflow.build_dir.any_platform.dockerfile.content = df_content

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

    plugin.run()

    expected_images = [f'{REGISTRY}/{expected}:{tag}' for tag in ['foo', 'bar', '1.7', '1.7-99']]
    actual_images = [str(image) for image in workflow.data.tag_conf.images]
    assert sorted(actual_images) == sorted(expected_images)


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
