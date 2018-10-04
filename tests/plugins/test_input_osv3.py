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
from atomic_reactor.constants import (PLUGIN_BUMP_RELEASE_KEY,
                                      PLUGIN_DELETE_FROM_REG_KEY,
                                      PLUGIN_DISTGIT_FETCH_KEY,
                                      PLUGIN_DOCKERFILE_CONTENT_KEY,
                                      PLUGIN_FETCH_MAVEN_KEY,
                                      PLUGIN_INJECT_PARENT_IMAGE_KEY,
                                      PLUGIN_KOJI_IMPORT_PLUGIN_KEY,
                                      PLUGIN_KOJI_PARENT_KEY,
                                      PLUGIN_KOJI_PROMOTE_PLUGIN_KEY,
                                      PLUGIN_KOJI_TAG_BUILD_KEY,
                                      PLUGIN_KOJI_UPLOAD_PLUGIN_KEY,
                                      PLUGIN_PULP_PUBLISH_KEY,
                                      PLUGIN_PULP_PULL_KEY,
                                      PLUGIN_PULP_PUSH_KEY,
                                      PLUGIN_PULP_SYNC_KEY,
                                      PLUGIN_PULP_TAG_KEY,
                                      PLUGIN_RESOLVE_COMPOSES_KEY,
                                      PLUGIN_SENDMAIL_KEY,
                                      PLUGIN_VERIFY_MEDIA_KEY)
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
                'name': PLUGIN_DOCKERFILE_CONTENT_KEY,
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
            {'name': PLUGIN_BUMP_RELEASE_KEY, },
            {'name': PLUGIN_FETCH_MAVEN_KEY, },
            {'name': PLUGIN_DISTGIT_FETCH_KEY, },
            {'name': PLUGIN_DOCKERFILE_CONTENT_KEY, },
            {'name': PLUGIN_INJECT_PARENT_IMAGE_KEY, },
            {'name': PLUGIN_KOJI_PARENT_KEY, },
            {'name': PLUGIN_RESOLVE_COMPOSES_KEY, },
            {'name': 'after', },
        ],
        'postbuild_plugins': [
            {'name': 'before', },
            {'name': PLUGIN_KOJI_UPLOAD_PLUGIN_KEY, },
            {'name': PLUGIN_PULP_PULL_KEY, },
            {'name': PLUGIN_PULP_PUSH_KEY, },
            {'name': PLUGIN_PULP_SYNC_KEY, },
            {'name': PLUGIN_PULP_TAG_KEY, },
            {'name': 'after', },
        ],
        'exit_plugins': [
            {'name': 'before', },
            {'name': PLUGIN_DELETE_FROM_REG_KEY, },
            {'name': PLUGIN_KOJI_IMPORT_PLUGIN_KEY, },
            {'name': PLUGIN_KOJI_PROMOTE_PLUGIN_KEY, },
            {'name': PLUGIN_KOJI_TAG_BUILD_KEY, },
            {'name': PLUGIN_PULP_PUBLISH_KEY, },
            {'name': PLUGIN_PULP_PULL_KEY, },
            {'name': PLUGIN_SENDMAIL_KEY, },
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
            {'name': PLUGIN_PULP_PUSH_KEY, },
            {'name': 'after', },
        ],
        'exit_plugins': [
            {'name': 'before', },
            {'name': PLUGIN_DELETE_FROM_REG_KEY, },
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
            {'name': PLUGIN_PULP_SYNC_KEY, },
            {'name': 'after', },
        ],
        'exit_plugins': [
            {'name': 'before', },
            {'name': PLUGIN_DELETE_FROM_REG_KEY, },
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


@pytest.mark.parametrize(('orchestrator', 'arrangement', 'pulp', 'pulp_phase', 'verify_media',
                          'error_msg', 'expected_plugin'), [
    # verify media is never an option for arrangement 5 or worker builds
    (False, 5, False, None, False, None, None),
    (False, 5, True, None, False, None, None),
    (False, 6, False, None, False, None, None),
    (False, 6, True, None, False, None, None),
    (False, 6, False, None, True, None, None),
    (True, 5, False, None, False, None, None),
    (True, 5, True, None, False, None, None),
    (True, 5, True, 'postbuild_plugins', False, None, PLUGIN_PULP_PULL_KEY),
    (True, 5, False, 'postbuild_plugins', False, None, None),
    (True, 6, False, None, False, 'exit_pulp_pull or exit_verify_media_types required', None),
    (True, 6, True, None, False, 'exit_pulp_pull or exit_verify_media_types required', None),
    (True, 6, False, 'exit_plugins', False, 'exit_pulp_pull or exit_verify_media_types required',
     None),
    (True, 6, True, 'exit_plugins', False, None, PLUGIN_PULP_PULL_KEY),
    (True, 6, False, None, True, None, PLUGIN_VERIFY_MEDIA_KEY),
    (True, 6, True, None, True, None, PLUGIN_VERIFY_MEDIA_KEY),
    (True, 6, False, 'exit_plugins', True, None, PLUGIN_VERIFY_MEDIA_KEY),
    (True, 6, True, 'exit_plugins', True, None, PLUGIN_PULP_PULL_KEY),
])
def test_verify_media_warnings(orchestrator, arrangement, pulp, pulp_phase, verify_media,
                               error_msg, expected_plugin, caplog):
    build_type = 'worker'
    if orchestrator:
        build_type = 'orchestrator'
    postbuild = []
    if pulp_phase == 'postbuild_plugins':
        postbuild.append({'name': PLUGIN_PULP_PULL_KEY})
    exit_phase = []
    if pulp_phase == 'exit_plugins':
        exit_phase.append({'name': PLUGIN_PULP_PULL_KEY})
    if verify_media:
        exit_phase.append({'name': PLUGIN_VERIFY_MEDIA_KEY})
    plugins_json = {
        'arrangement_version': arrangement,
        'build_json_dir': 'inputs',
        'build_type': build_type,
        'git_ref': 'test',
        'git_uri': 'test',
        'user': 'user',
        'postbuild_plugins': postbuild,
        'exit_plugins': exit_phase
    }

    if pulp:
        minimal_config = dedent("""\
            version: 1
            pulp:
                name: my-pulp
                auth:
                    password: testpasswd
                    username: testuser
            koji:
                hub_url: https://koji.com/hub
                root_url: https://koji.com/hub
                auth:
                    ssl_certs_dir: testpasswd
                    proxyuser: testuser
            registries:
            - url: https://container-registry.example.com/v1
            auth:
                cfg_path: /var/run/secrets/atomic-reactor/v1-registry-dockercfg
        """)
    else:
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
    results = plugin.run()

    if error_msg:
        assert error_msg in caplog.text()
    else:
        assert 'exit_verify_media_types' not in caplog.text()
        if expected_plugin:
            expected_phase = pulp_phase or 'exit_plugins'
            assert results[expected_phase] == [{'name': expected_plugin}]
