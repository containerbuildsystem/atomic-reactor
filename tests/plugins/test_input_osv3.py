"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals
import os
import json
import types
from textwrap import dedent

from atomic_reactor.plugins.input_osv3 import OSv3InputPlugin
from osbs.api import OSBS
from tests.constants import REACTOR_CONFIG_MAP

import pytest
from flexmock import flexmock
from jsonschema import ValidationError


def test_doesnt_fail_if_no_plugins():
    mock_env = {
        'BUILD': '{}',
        'SOURCE_URI': 'https://github.com/foo/bar.git',
        'SOURCE_REF': 'master',
        'OUTPUT_IMAGE': 'asdf:fdsa',
        'OUTPUT_REGISTRY': 'localhost:5000',
        'ATOMIC_REACTOR_PLUGINS': '{}',
    }
    flexmock(os, environ=mock_env)

    plugin = OSv3InputPlugin()
    assert plugin.run()['openshift_build_selflink'] is None


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
        'ATOMIC_REACTOR_PLUGINS': '{}',
    }
    flexmock(os, environ=mock_env)

    plugin = OSv3InputPlugin()
    assert plugin.run()['openshift_build_selflink'] == expected


def enable_plugins_configuration(plugins_json):
    # flexmock won't mock a non-existent method, so add it if necessary
    try:
        getattr(OSBS, 'render_plugins_configuration')
    except AttributeError:
        setattr(OSBS, 'render_plugins_configuration',
                types.MethodType(lambda x: x, 'render_plugins_configuration'))
    (flexmock(OSBS)
        .should_receive('render_plugins_configuration')
        .and_return(json.dumps(plugins_json)))


@pytest.mark.parametrize(('plugins_variable', 'valid'), [
    ('ATOMIC_REACTOR_PLUGINS', True),
    ('USER_PARAMS', True),
    ('DOCK_PLUGINS', False),
])
def test_plugins_variable(plugins_variable, valid):
    plugins_json = {
        'postbuild_plugins': [],
    }

    mock_env = {
        'BUILD': '{}',
        'SOURCE_URI': 'https://github.com/foo/bar.git',
        'SOURCE_REF': 'master',
        'OUTPUT_IMAGE': 'asdf:fdsa',
        'OUTPUT_REGISTRY': 'localhost:5000',
        plugins_variable: json.dumps(plugins_json),
    }

    if plugins_variable == 'USER_PARAMS':
        mock_env['REACTOR_CONFIG'] = REACTOR_CONFIG_MAP
        enable_plugins_configuration(plugins_json)
        mock_env.update({
            plugins_variable: json.dumps({
                'build_json_dir': 'inputs',
                'build_type': 'orchestrator',
                'git_ref': 'test',
                'git_uri': 'test',
                'user': 'user'
            }),
        })

    flexmock(os, environ=mock_env)

    plugin = OSv3InputPlugin()
    if valid:
        assert plugin.run()['postbuild_plugins'] is not None
    else:
        with pytest.raises(RuntimeError):
            plugin.run()


def test_remove_dockerfile_content():
    plugins_json = {
        'prebuild_plugins': [
            {
                'name': 'before',
            },
            {
                'name': 'dockerfile_content',
            },
            {
                'name': 'after',
            },
        ]
    }

    mock_env = {
        'BUILD': '{}',
        'SOURCE_URI': 'https://github.com/foo/bar.git',
        'SOURCE_REF': 'master',
        'OUTPUT_IMAGE': 'asdf:fdsa',
        'OUTPUT_REGISTRY': 'localhost:5000',
        'ATOMIC_REACTOR_PLUGINS': json.dumps(plugins_json),
    }
    flexmock(os, environ=mock_env)

    plugin = OSv3InputPlugin()
    assert plugin.run()['prebuild_plugins'] == [
        {
            'name': 'before',
        },
        {
            'name': 'after',
        },
    ]


def test_remove_everything():
    plugins_json = {
        'build_json_dir': 'inputs',
        'build_type': 'orchestrator',
        'git_ref': 'test',
        'git_uri': 'test',
        'user': 'user',
        'prebuild_plugins': [
            {'name': 'before', },
            {'name': 'bump_release', },
            {'name': 'fetch_maven_artifacts', },
            {'name': 'distgit_fetch_artefacts', },
            {'name': 'dockerfile_content', },
            {'name': 'inject_parent_image', },
            {'name': 'koji_parent', },
            {'name': 'resolve_composes', },
            {'name': 'after', },
        ],
        'postbuild_plugins': [
            {'name': 'before', },
            {'name': 'koji_upload', },
            {'name': 'pulp_pull', },
            {'name': 'pulp_push', },
            {'name': 'pulp_sync', },
            {'name': 'pulp_tag', },
            {'name': 'after', },
        ],
        'exit_plugins': [
            {'name': 'before', },
            {'name': 'delete_from_registry', },
            {'name': 'koji_import', },
            {'name': 'koji_promote', },
            {'name': 'koji_tag_build', },
            {'name': 'pulp_publish', },
            {'name': 'pulp_pull', },
            {'name': 'sendmail', },
            {'name': 'after', },
        ]
    }
    minimal_config = dedent("""\
        version: 1
    """)

    mock_env = {
        'BUILD': '{}',
        'SOURCE_URI': 'https://github.com/foo/bar.git',
        'SOURCE_REF': 'master',
        'OUTPUT_IMAGE': 'asdf:fdsa',
        'OUTPUT_REGISTRY': 'localhost:5000',
        'USER_PARAMS': json.dumps(plugins_json),
        'REACTOR_CONFIG': minimal_config
    }
    flexmock(os, environ=mock_env)
    enable_plugins_configuration(plugins_json)

    plugin = OSv3InputPlugin()
    plugins = plugin.run()
    for phase in ('prebuild_plugins', 'postbuild_plugins', 'exit_plugins'):
        assert plugins[phase] == [
            {'name': 'before', },
            {'name': 'after', },
        ]


def test_remove_v1_pulp_and_exit_delete():
    plugins_json = {
        'build_json_dir': 'inputs',
        'build_type': 'orchestrator',
        'git_ref': 'test',
        'git_uri': 'test',
        'user': 'user',
        'postbuild_plugins': [
            {'name': 'before', },
            {'name': 'pulp_push', },
            {'name': 'after', },
        ],
        'exit_plugins': [
            {'name': 'before', },
            {'name': 'delete_from_registry', },
            {'name': 'after', },
        ],
    }
    minimal_config = dedent("""\
        version: 1
        pulp:
            name: my-pulp
            auth:
                password: testpasswd
                username: testuser
        content_versions:
        - v2
        registries:
        - url: https://container-registry.example.com/v2
        auth:
            cfg_path: /var/run/secrets/atomic-reactor/v2-registry-dockercfg
        source_registry:
            url: https://container-registry.example.com/v2
            insecure: True
    """)

    mock_env = {
        'BUILD': '{}',
        'SOURCE_URI': 'https://github.com/foo/bar.git',
        'SOURCE_REF': 'master',
        'OUTPUT_IMAGE': 'asdf:fdsa',
        'OUTPUT_REGISTRY': 'localhost:5000',
        'USER_PARAMS': json.dumps(plugins_json),
        'REACTOR_CONFIG': minimal_config
    }
    flexmock(os, environ=mock_env)
    enable_plugins_configuration(plugins_json)

    plugin = OSv3InputPlugin()
    plugins = plugin.run()
    for phase in ('postbuild_plugins', 'exit_plugins'):
        assert plugins[phase] == [
            {'name': 'before', },
            {'name': 'after', },
        ]


def test_remove_v2_pulp():
    plugins_json = {
        'build_json_dir': 'inputs',
        'build_type': 'orchestrator',
        'git_ref': 'test',
        'git_uri': 'test',
        'user': 'user',
        'postbuild_plugins': [
            {'name': 'before', },
            {'name': 'pulp_sync', },
            {'name': 'after', },
        ],
        'exit_plugins': [
            {'name': 'before', },
            {'name': 'delete_from_registry', },
            {'name': 'after', },
        ],
    }
    minimal_config = dedent("""\
        version: 1
        pulp:
            name: my-pulp
            auth:
                password: testpasswd
                username: testuser
        registries:
        - url: https://container-registry.example.com/v1
        auth:
            cfg_path: /var/run/secrets/atomic-reactor/v1-registry-dockercfg
    """)

    mock_env = {
        'BUILD': '{}',
        'SOURCE_URI': 'https://github.com/foo/bar.git',
        'SOURCE_REF': 'master',
        'OUTPUT_IMAGE': 'asdf:fdsa',
        'OUTPUT_REGISTRY': 'localhost:5000',
        'USER_PARAMS': json.dumps(plugins_json),
        'REACTOR_CONFIG': minimal_config
    }
    flexmock(os, environ=mock_env)
    enable_plugins_configuration(plugins_json)

    plugin = OSv3InputPlugin()
    plugins = plugin.run()
    assert plugins['postbuild_plugins'] == [
        {'name': 'before', },
        {'name': 'after', },
    ]


@pytest.mark.parametrize(('override', 'valid'), [
    ('invalid_override', False),
    ({'version': 1}, True),
    (None, True),
])
@pytest.mark.parametrize('buildtype', [
    'worker', 'orchestrator'
])
def test_validate_reactor_config_override(override, valid, buildtype):
    plugins_json = {
        'postbuild_plugins': [],
    }

    user_params = {
        'build_json_dir': 'inputs',
        'build_type': buildtype,
        'git_ref': 'test',
        'git_uri': 'test',
        'user': 'user',
        'reactor_config_map': REACTOR_CONFIG_MAP,
    }
    if override:
        user_params['reactor_config_override'] = override
    mock_env = {
        'BUILD': '{}',
        'SOURCE_URI': 'https://github.com/foo/bar.git',
        'SOURCE_REF': 'master',
        'OUTPUT_IMAGE': 'asdf:fdsa',
        'OUTPUT_REGISTRY': 'localhost:5000',
        'REACTOR_CONFIG': REACTOR_CONFIG_MAP,
        'USER_PARAMS': json.dumps(user_params)
    }

    enable_plugins_configuration(plugins_json)

    flexmock(os, environ=mock_env)

    plugin = OSv3InputPlugin()
    if valid:
        plugin.run()
    else:
        with pytest.raises(ValidationError):
            plugin.run()
