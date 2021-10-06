"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import os
import sys
import yaml

from atomic_reactor.constants import PLUGIN_CHECK_AND_SET_PLATFORMS_KEY, REPO_CONTAINER_CONFIG
import atomic_reactor.utils.koji as koji_util
from atomic_reactor.source import SourceConfig
from atomic_reactor import util

from flexmock import flexmock
import pytest

from tests.mock_env import MockEnv


KOJI_TARGET = "target"


# ClientSession is xmlrpc instance, we need to mock it explicitly
def mock_session(platforms):
    arches = None
    if platforms:
        arches = ' '.join(sorted(platforms.keys()))
    last_event_id = 456
    build_target = {
        'build_tag': 'build-tag',
        'name': 'target-name',
        'dest_tag_name': 'dest-tag'
    }
    session = flexmock()
    (session
        .should_receive('getLastEvent')
        .and_return({'id': last_event_id}))
    (session
        .should_receive('getBuildTarget')
        .with_args('target', event=last_event_id)
        .and_return(build_target))
    (session
        .should_receive('getBuildConfig')
        .with_args('build-tag', event=last_event_id)
        .and_return({'arches': arches}))

    return session


class MockSource(object):
    def __init__(self, tmpdir):
        self.path = str(tmpdir)
        self.dockerfile_path = str(tmpdir)
        self._config = None

    def get_build_file_path(self):
        return self.path, self.path

    @property
    def config(self):
        self._config = self._config or SourceConfig(self.path)
        return self._config


def make_reactor_config_map(platforms):
    clusters = {}
    if platforms:
        for platform, enabled in platforms.items():
            clusters[platform] = [{'enabled': enabled, 'max_concurrent_builds': 1,
                                   'name': platform}]
        return {'version': 1, 'koji': {'auth': {}, 'hub_url': 'test'}, 'clusters': clusters}
    else:
        return {'version': 1, 'koji': {'auth': {}, 'hub_url': 'test'}}


def write_container_yaml(tmpdir, platform_exclude='', platform_only=''):
    platforms_dict = {}
    if platform_exclude != '':
        platforms_dict['platforms'] = {}
        platforms_dict['platforms']['not'] = platform_exclude
    if platform_only != '':
        if 'platforms' not in platforms_dict:
            platforms_dict['platforms'] = {}
        platforms_dict['platforms']['only'] = platform_only

    container_path = os.path.join(str(tmpdir), REPO_CONTAINER_CONFIG)
    with open(container_path, 'w') as f:
        f.write(yaml.safe_dump(platforms_dict))
        f.flush()


def mock_env(tmpdir, labels=None):
    labels = labels or {}
    env = (
        MockEnv()
        .for_plugin('prebuild', PLUGIN_CHECK_AND_SET_PLATFORMS_KEY)
        .set_scratch(labels.get('scratch', False))
        .set_isolated(labels.get('isolated', False))
    )
    env.workflow.source = MockSource(tmpdir)
    return env


def teardown_function(function):
    sys.modules.pop('pre_check_and_set_platforms', None)


@pytest.mark.parametrize(('platforms', 'platform_exclude', 'platform_only', 'result'), [
    (None, '', 'ppc64le', None),
    ({'x86_64': True, 'ppc64le': True},
     '', 'ppc64le', ['ppc64le']),
    ({'x86_64': True, 'spam': True, 'bacon': True, 'toast': True, 'ppc64le': True},
     ['spam', 'bacon', 'eggs', 'toast'], '',
     ['x86_64', 'ppc64le']),
    ({'ppc64le': True, 'spam': True, 'bacon': True, 'toast': True},
     ['spam', 'bacon', 'eggs', 'toast'], 'ppc64le',
     ['ppc64le']),
    ({'x86_64': True, 'bacon': True, 'toast': True},
     'toast', ['x86_64', 'ppc64le'], ['x86_64']),
    ({'x86_64': True, 'toast': True},
     'toast', 'x86_64', ['x86_64']),
    ({'x86_64': True, 'spam': True, 'bacon': True, 'toast': True},
     ['spam', 'bacon', 'eggs', 'toast'], ['x86_64', 'ppc64le'], ['x86_64']),
    ({'x86_64': True, 'ppc64le': True},
     '', '', ['x86_64', 'ppc64le'])
])
def test_check_and_set_platforms(tmpdir, caplog, user_params,
                                 platforms, platform_exclude, platform_only, result):
    write_container_yaml(tmpdir, platform_exclude, platform_only)

    env = mock_env(tmpdir)

    build_json = {'metadata': {'labels': {}}}
    flexmock(util).should_receive('get_build_json').and_return(build_json)

    session = mock_session(platforms)
    flexmock(koji_util).should_receive('create_koji_session').and_return(session)

    env.set_reactor_config(make_reactor_config_map(platforms))
    env.set_plugin_args({'koji_target': KOJI_TARGET})

    runner = env.create_runner()

    plugin_result = runner.run()
    if platforms:
        koji_msg = "Koji platforms are {0}".format(sorted(platforms.keys()))
        assert koji_msg in caplog.text
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY]
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] == set(result)
    else:
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] is None
        assert "No platforms found in koji target" in caplog.text


@pytest.mark.parametrize(('labels', 'platforms', 'orchestrator_platforms', 'platform_only',
                          'result'), [
    ({}, None,
     None, '', None),
    ({}, {'x86_64': True, 'arm64': True},
     ['spam', 'bacon'], '', ['arm64', 'x86_64']),
    ({'isolated': True}, {'spam': True, 'bacon': True},
     ['x86_64', 'arm64'], '', ['arm64', 'x86_64']),
    ({'isolated': True}, {'x86_64': True, 'arm64': True},
     None, '', ['arm64', 'x86_64']),
    ({'isolated': True}, None,
     ['x86_64', 'arm64'], '', None),
    ({'scratch': True}, {'spam': True, 'bacon': True},
     ['x86_64', 'arm64'], '', ['arm64', 'x86_64']),
    ({'scratch': True}, {'x86_64': True, 'arm64': True},
     None, '', ['arm64', 'x86_64']),
    ({'scratch': True}, None,
     ['x86_64', 'arm64'], '', None),
    ({'scratch': True}, {'x86_64': True, 'arm64': True},
     ['x86_64', 'arm64'], 'x86_64', ['x86_64']),
    ({'scratch': True}, {'x86_64': True, 'arm64': True, 's390x': True},
     ['x86_64', 'arm64'], 'x86_64', ['x86_64', 'arm64']),
])
def test_check_isolated_or_scratch(tmpdir, caplog, user_params,
                                   labels, platforms, orchestrator_platforms, platform_only,
                                   result):
    write_container_yaml(tmpdir, platform_only=platform_only)

    env = mock_env(tmpdir, labels=labels)
    if orchestrator_platforms:
        env.set_orchestrator_platforms(platforms=orchestrator_platforms)

    session = mock_session(platforms)
    flexmock(koji_util).should_receive('create_koji_session').and_return(session)

    env.set_reactor_config(make_reactor_config_map(platforms))
    env.set_plugin_args({'koji_target': KOJI_TARGET})

    runner = env.create_runner()

    plugin_result = runner.run()
    if platforms:
        koji_msg = "Koji platforms are {0}".format(sorted(platforms.keys()))
        assert koji_msg in caplog.text
        diffplat = orchestrator_platforms and set(platforms.keys()) != set(orchestrator_platforms)
        if labels and diffplat:
            sort_platforms = sorted(orchestrator_platforms)
            user_msg = "Received user specified platforms {0}".format(sort_platforms)
            assert user_msg in caplog.text
    else:
        assert "No platforms found in koji target" in caplog.text

    if result:
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY]
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] == set(result)
    else:
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] is None


@pytest.mark.parametrize(('platforms', 'platform_only', 'result'), [
    (None, 'ppc64le', None),
    ({'x86_64': True, 'ppc64le': True}, '', ['x86_64', 'ppc64le']),
    ({'x86_64': True, 'ppc64le': True}, 'ppc64le', ['ppc64le']),
])
def test_check_and_set_platforms_no_koji(tmpdir, caplog, user_params,
                                         platforms, platform_only, result):
    write_container_yaml(tmpdir, platform_only=platform_only)

    env = mock_env(tmpdir)

    if platforms:
        env.set_orchestrator_platforms(platforms.keys())

    build_json = {'metadata': {'labels': {}}}
    flexmock(util).should_receive('get_build_json').and_return(build_json)

    env.set_reactor_config(make_reactor_config_map(platforms))

    runner = env.create_runner()

    if platforms:
        plugin_result = runner.run()
        # Build up the message to avoid wrapping
        no_koji_msg = "No koji platforms. "
        platform_msg = "User specified platforms are {0}".format(sorted(platforms.keys()))
        user_msg = no_koji_msg + platform_msg
        assert user_msg in caplog.text
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY]
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] == set(result)
    else:
        with pytest.raises(Exception) as e:
            runner.run()
        assert "no koji target or platform list" in str(e.value)


@pytest.mark.parametrize(('platforms', 'platform_only'), [
    ({'x86_64': True}, 'ppc64le'),
    ({'x86_64': True, 'ppc64le': True}, 's390x'),
    ({'s390x': True, 'ppc64le': True}, 'x86_64'),
])
def test_check_and_set_platforms_no_platforms_in_limits(tmpdir, caplog, user_params,
                                                        platforms, platform_only):
    write_container_yaml(tmpdir, platform_only=platform_only)

    env = mock_env(tmpdir)

    if platforms:
        env.set_orchestrator_platforms(platforms.keys())

    build_json = {'metadata': {'labels': {}}}
    flexmock(util).should_receive('get_build_json').and_return(build_json)
    env.set_reactor_config(make_reactor_config_map(platforms))

    runner = env.create_runner()

    with pytest.raises(Exception) as e:
        runner.run()

    assert "platforms in limits : %s" % set() in caplog.text
    assert "platforms in limits are empty" in caplog.text
    assert "No platforms to build for" in str(e.value)


@pytest.mark.parametrize(('platforms', 'platform_only', 'cluster_platforms', 'result'), [
    ('x86_64 ppc64le', '', {'x86_64': True}, ['x86_64']),
    ('x86_64 ppc64le arm64', ['x86_64', 'arm64'], {'x86_64': True}, ['x86_64']),
])
def test_platforms_from_cluster_config(tmpdir, user_params,
                                       platforms, platform_only, cluster_platforms, result):
    write_container_yaml(tmpdir, platform_only=platform_only)

    env = mock_env(tmpdir)

    if platforms:
        env.set_orchestrator_platforms(platforms.split())

    build_json = {'metadata': {'labels': {}}}
    flexmock(util).should_receive('get_build_json').and_return(build_json)

    env.set_reactor_config(make_reactor_config_map(cluster_platforms))

    runner = env.create_runner()

    plugin_result = runner.run()
    if platforms:
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY]
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] == set(result)
    else:
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] is None


@pytest.mark.parametrize(('koji_platforms', 'cluster_platforms', 'result', 'skips', 'fails'), [
    (None, None, None, None, None),
    (['x86_64'], None, None, None, 'no_platforms'),
    (['x86_64'], {'ppc64le': True}, None, None, 'no_platforms'),
    (['x86_64', 'ppc64le'], {'x86_64': True, 'ppc64le': True}, ['x86_64', 'ppc64le'], None, None),
    (['x86_64', 'ppc64le'], {'x86_64': False, 'ppc64le': True}, None, None, 'disabled'),
    (['x86_64', 'ppc64le'], {'x86_64': False, 'ppc64le': False}, None, None, 'disabled'),
    (['x86_64', 'ppc64le'], {'x86_64': True}, ['x86_64'], ['ppc64le'], None),
    (['x86_64', 'ppc64le', 's390x'], {'x86_64': True}, ['x86_64'], ['ppc64le', 's390x'], None),
])
def test_disabled_clusters(tmpdir, caplog, user_params, koji_platforms,
                           cluster_platforms, result, skips, fails):
    write_container_yaml(tmpdir)

    env = mock_env(tmpdir)

    build_json = {'metadata': {'labels': {}}}
    flexmock(util).should_receive('get_build_json').and_return(build_json)

    new_koji_platforms = None
    if koji_platforms:
        new_koji_platforms = {k: True for k in koji_platforms}
    session = mock_session(new_koji_platforms)
    flexmock(koji_util).should_receive('create_koji_session').and_return(session)

    env.set_reactor_config(make_reactor_config_map(cluster_platforms))
    env.set_plugin_args({'koji_target': KOJI_TARGET})

    runner = env.create_runner()

    if fails:
        with pytest.raises(Exception) as e:
            runner.run()

        if fails == 'no_platforms':
            msg = 'No platforms to build for'
        elif fails == 'disabled':
            msg = 'Platforms specified in config map, but have all clusters disabled'
        assert msg in str(e.value)
    else:
        plugin_result = runner.run()

        if koji_platforms:
            koji_msg = "Koji platforms are {0}".format(sorted(koji_platforms))
            assert koji_msg in caplog.text
            assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY]
            assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] == set(result)

            if skips:
                for skip in skips:
                    msg = "No cluster found for platform '{}' in reactor config map, " \
                          "skipping".format(skip)
                    assert msg in caplog.text

        else:
            assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] is None
            assert "No platforms found in koji target" in caplog.text
