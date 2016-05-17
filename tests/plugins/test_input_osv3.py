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


@pytest.mark.parametrize('plugins_variable', ['ATOMIC_REACTOR_PLUGINS', 'DOCK_PLUGINS'])
def test_plugins_variable(plugins_variable):
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
    flexmock(os, environ=mock_env)

    plugin = OSv3InputPlugin()
    assert plugin.run()['postbuild_plugins'] is not None
