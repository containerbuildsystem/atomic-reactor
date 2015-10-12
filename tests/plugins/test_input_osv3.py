"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals
import os
import json

from atomic_reactor.plugins.input_osv3 import OSv3InputPlugin

import pytest
from flexmock import flexmock

from tests.constants import LOCALHOST_REGISTRY

@pytest.mark.parametrize('prebuild_json,expected_json', [
    ([],
     [{ 'name': 'pull_base_image' }]
    ),
    ([{ 'name': 'pull_base_image' }],
     [{ 'name': 'pull_base_image' }]
    ),
    ([{ 'name': 'pull_base_image', 'args': { 'a': 'b' }}],
     [{ 'name': 'pull_base_image', 'args': { 'a': 'b' }}]
    ),
    ([{ 'name': 'change_source_registry' }],
     [{ 'name': 'pull_base_image' }]
    ),
    ([{ 'name': 'change_source_registry',
        'args': { 'registry_uri': 'localhost:666', 'insecure_registry': True }}],
     [{ 'name': 'pull_base_image',
        'args': { 'parent_registry': 'localhost:666', 'parent_registry_insecure': True }}]
    ),
    ([{ 'name': 'change_source_registry' }, { 'name': 'pull_base_image', 'args': { 'a': 'b' }}],
     [{ 'name': 'pull_base_image', 'args': { 'a': 'b' }}]
    ),
])
def test_prebuild_plugins_rewrite(prebuild_json, expected_json):
    plugins_json = {
        'prebuild_plugins': prebuild_json,
    }

    mock_env = {
        'BUILD': '{}',
        'SOURCE_URI': 'https://github.com/foo/bar.git',
        'SOURCE_REF': 'master',
        'OUTPUT_IMAGE': 'asdf:fdsa',
        'OUTPUT_REGISTRY': 'localhost:5000',
        'DOCK_PLUGINS': json.dumps(plugins_json),
    }
    flexmock(os, environ=mock_env)

    plugin = OSv3InputPlugin()
    assert plugin.run()['prebuild_plugins'] == expected_json


@pytest.mark.parametrize('output_registry,postbuild_json,expected_json', [
    (LOCALHOST_REGISTRY,
     [],
     [{ 'name': 'tag_and_push', 'args': { 'registries': { LOCALHOST_REGISTRY: { 'insecure': True }}}}]
    ),
    (LOCALHOST_REGISTRY,
     [{ 'name': 'tag_and_push' }],
     [{ 'name': 'tag_and_push', 'args': { 'registries': { LOCALHOST_REGISTRY: { 'insecure': True }}}}]
    ),
    (LOCALHOST_REGISTRY,
     [{ 'name': 'tag_and_push', 'args': { 'registries': { 'foo': { 'insecure': True }}}}],
     [{ 'name': 'tag_and_push', 'args': { 'registries': { 'foo': { 'insecure': True }}}}]
    ),
    (None,
     [{ 'name': 'tag_and_push', 'args': { 'registries': { 'foo': { 'insecure': True }}}}],
     [{ 'name': 'tag_and_push', 'args': { 'registries': { 'foo': { 'insecure': True }}}}]
    ),
    (None,
     [],
     []
    ),
])
def test_postbuild_plugins_rewrite(output_registry, postbuild_json, expected_json):
    plugins_json = {
        'postbuild_plugins': postbuild_json,
    }

    mock_env = {
        'BUILD': '{}',
        'SOURCE_URI': 'https://github.com/foo/bar.git',
        'SOURCE_REF': 'master',
        'OUTPUT_IMAGE': 'asdf:fdsa',
        'OUTPUT_REGISTRY': output_registry,
        'DOCK_PLUGINS': json.dumps(plugins_json),
    }
    flexmock(os, environ=mock_env)

    plugin = OSv3InputPlugin()
    assert plugin.run()['postbuild_plugins'] == expected_json

def test_doesnt_fail_if_no_plugins():
    mock_env = {
        'BUILD': '{}',
        'SOURCE_URI': 'https://github.com/foo/bar.git',
        'SOURCE_REF': 'master',
        'OUTPUT_IMAGE': 'asdf:fdsa',
        'OUTPUT_REGISTRY': 'localhost:5000',
        'DOCK_PLUGINS': '{}',
    }
    flexmock(os, environ=mock_env)

    plugin = OSv3InputPlugin()
    assert plugin.run()['prebuild_plugins'] == [{'name': 'pull_base_image'}]


@pytest.mark.parametrize('build, expected', [
    ('{"metadata": {"selfLink": "/foo/bar"}}', '/foo/bar'),
    ('{"metadata": {}}', None),
    ('{}', None),
])
def test_sets_selflink(build, expected):
    mock_env = {
        'BUILD': build,
        'SOURCE_URI': 'https://github.com/foo/bar.git',
        'SOURCE_REF': 'master',
        'OUTPUT_IMAGE': 'asdf:fdsa',
        'OUTPUT_REGISTRY': 'localhost:5000',
        'DOCK_PLUGINS': '{}',
    }
    flexmock(os, environ=mock_env)

    plugin = OSv3InputPlugin()
    assert plugin.run()['openshift_build_selflink'] == expected
