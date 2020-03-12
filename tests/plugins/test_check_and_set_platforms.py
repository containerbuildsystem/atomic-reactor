"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import
import os
import sys
import yaml

from atomic_reactor.constants import (PLUGIN_CHECK_AND_SET_PLATFORMS_KEY, REPO_CONTAINER_CONFIG,
                                      PLUGIN_BUILD_ORCHESTRATE_KEY)
import atomic_reactor.plugins.pre_reactor_config as reactor_config
import atomic_reactor.utils.koji as koji_util
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.util import ImageName
from atomic_reactor.source import SourceConfig
from atomic_reactor import util
from flexmock import flexmock
import pytest
from tests.constants import SOURCE, MOCK
if MOCK:
    from tests.docker_mock import mock_docker


class X(object):
    pass


KOJI_TARGET = "target"


# ClientSession is xmlrpc instance, we need to mock it explicitly
def mock_session(platforms):
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
        .and_return({'arches': platforms}))

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


class MockClusterConfig(object):
    enabled = True


class MockConfig(object):
    def __init__(self, platforms):
        if platforms:
            self.platforms = set(platforms.split())
        else:
            self.platforms = ['x86_64']

    def get_enabled_clusters_for_platform(self, platform):
        if platform in self.platforms:
            return MockClusterConfig
        else:
            return []


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


def set_orchestrator_platforms(workflow, orchestrator_platforms):
    workflow.buildstep_plugins_conf = [{'name': PLUGIN_BUILD_ORCHESTRATE_KEY,
                                        'args': {'platforms': orchestrator_platforms}}]


def prepare(tmpdir):
    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow("test-image", source=SOURCE)
    setattr(workflow, 'builder', X())
    source = MockSource(tmpdir)

    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='21'))
    setattr(workflow.builder, 'source', source)
    setattr(workflow, 'source', source)

    return tasker, workflow


def teardown_function(function):
    sys.modules.pop('pre_check_and_set_platforms', None)


@pytest.mark.parametrize(('platforms', 'platform_exclude', 'platform_only', 'result'), [
    (None, '', 'ppc64le', None),
    ('x86_64 ppc64le', '', 'ppc64le', ['ppc64le']),
    ('x86_64 spam bacon toast ppc64le', ['spam', 'bacon', 'eggs', 'toast'], '',
     ['x86_64', 'ppc64le']),
    ('ppc64le spam bacon toast', ['spam', 'bacon', 'eggs', 'toast'], 'ppc64le',
     ['ppc64le']),
    ('x86_64 bacon toast', 'toast', ['x86_64', 'ppc64le'], ['x86_64']),
    ('x86_64 toast', 'toast', 'x86_64', ['x86_64']),
    ('x86_64 spam bacon toast', ['spam', 'bacon', 'eggs', 'toast'], ['x86_64', 'ppc64le'],
     ['x86_64']),
    ('x86_64 ppc64le', '', '', ['x86_64', 'ppc64le'])
])
def test_check_and_set_platforms(tmpdir, caplog, platforms, platform_exclude, platform_only,
                                 result):
    write_container_yaml(tmpdir, platform_exclude, platform_only)

    tasker, workflow = prepare(tmpdir)

    build_json = {'metadata': {'labels': {}}}
    flexmock(util).should_receive('get_build_json').and_return(build_json)

    session = mock_session(platforms)
    mock_koji_config = {
        'auth': {},
        'hub_url': 'test',
    }
    flexmock(reactor_config).should_receive('get_koji').and_return(mock_koji_config)
    flexmock(koji_util).should_receive('create_koji_session').and_return(session)

    mock_config = MockConfig(platforms)
    flexmock(reactor_config).should_receive('get_config').and_return(mock_config)

    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': PLUGIN_CHECK_AND_SET_PLATFORMS_KEY,
        'args': {'koji_target': KOJI_TARGET},
    }])

    plugin_result = runner.run()
    if platforms:
        koji_msg = "Koji platforms are {0}".format(sorted(platforms.split()))
        assert koji_msg in caplog.text
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY]
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] == set(result)
    else:
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] is None
        assert "No platforms found in koji target" in caplog.text


@pytest.mark.parametrize(('labels', 'platforms', 'orchestrator_platforms', 'platform_only',
                          'result'), [
    ({}, None, None, '', None),
    ({}, 'x86_64 arm64', ['spam', 'bacon'], '', ['arm64', 'x86_64']),
    ({'isolated': True}, 'spam bacon', ['x86_64', 'arm64'], '', ['arm64', 'x86_64']),
    ({'isolated': True}, 'x86_64 arm64', None, '', ['arm64', 'x86_64']),
    ({'isolated': True}, None, ['x86_64', 'arm64'], '', None),
    ({'scratch': True}, 'spam bacon', ['x86_64', 'arm64'], '', ['arm64', 'x86_64']),
    ({'scratch': True}, 'x86_64 arm64', None, '', ['arm64', 'x86_64']),
    ({'scratch': True}, None, ['x86_64', 'arm64'], '', None),
    ({'scratch': True}, 'x86_64 arm64', ['x86_64', 'arm64'], 'x86_64', ['x86_64']),
    ({'scratch': True}, 'x86_64 arm64 s390x', ['x86_64', 'arm64'], 'x86_64', ['x86_64', 'arm64']),
])
def test_check_isolated_or_scratch(tmpdir, caplog, labels, platforms,
                                   orchestrator_platforms, platform_only, result):
    write_container_yaml(tmpdir, platform_only=platform_only)

    tasker, workflow = prepare(tmpdir)
    if orchestrator_platforms:
        set_orchestrator_platforms(workflow, orchestrator_platforms)

    build_json = {'metadata': {'labels': labels}}
    flexmock(util).should_receive('get_build_json').and_return(build_json)

    session = mock_session(platforms)
    mock_koji_config = {
        'auth': {},
        'hub_url': 'test',
    }
    flexmock(reactor_config).should_receive('get_koji').and_return(mock_koji_config)
    flexmock(koji_util).should_receive('create_koji_session').and_return(session)

    mock_config = MockConfig(platforms)
    flexmock(reactor_config).should_receive('get_config').and_return(mock_config)

    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': PLUGIN_CHECK_AND_SET_PLATFORMS_KEY,
        'args': {'koji_target': KOJI_TARGET},
    }])

    plugin_result = runner.run()
    if platforms:
        koji_msg = "Koji platforms are {0}".format(sorted(platforms.split()))
        assert koji_msg in caplog.text
        diffplat = orchestrator_platforms and set(platforms.split()) != set(orchestrator_platforms)
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
    ('x86_64 ppc64le', '', ['x86_64', 'ppc64le']),
    ('x86_64 ppc64le', 'ppc64le', ['ppc64le']),
])
def test_check_and_set_platforms_no_koji(tmpdir, caplog, platforms, platform_only, result):
    write_container_yaml(tmpdir, platform_only=platform_only)

    tasker, workflow = prepare(tmpdir)

    if platforms:
        set_orchestrator_platforms(workflow, platforms.split())

    build_json = {'metadata': {'labels': {}}}
    flexmock(util).should_receive('get_build_json').and_return(build_json)

    mock_config = MockConfig(platforms)
    flexmock(reactor_config).should_receive('get_config').and_return(mock_config)

    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': PLUGIN_CHECK_AND_SET_PLATFORMS_KEY,
    }])

    if platforms:
        plugin_result = runner.run()
        # Build up the message to avoid wrapping
        no_koji_msg = "No koji platforms. "
        platform_msg = "User specified platforms are {0}".format(sorted(platforms.split()))
        user_msg = no_koji_msg + platform_msg
        assert user_msg in caplog.text
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY]
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] == set(result)
    else:
        with pytest.raises(Exception) as e:
            plugin_result = runner.run()
            assert "no koji target or platform list" in str(e.value)


@pytest.mark.parametrize(('platforms', 'platform_only', 'cluster_platforms', 'result'), [
    ('x86_64 ppc64le', '', 'x86_64', ['x86_64']),
    ('x86_64 ppc64le arm64', ['x86_64', 'arm64'], 'x86_64', ['x86_64']),
])
def test_platforms_from_cluster_config(tmpdir, platforms, platform_only,
                                       cluster_platforms, result):
    write_container_yaml(tmpdir, platform_only=platform_only)

    tasker, workflow = prepare(tmpdir)

    if platforms:
        set_orchestrator_platforms(workflow, platforms.split())

    build_json = {'metadata': {'labels': {}}}
    flexmock(util).should_receive('get_build_json').and_return(build_json)

    mock_config = MockConfig(cluster_platforms)
    flexmock(reactor_config).should_receive('get_config').and_return(mock_config)

    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': PLUGIN_CHECK_AND_SET_PLATFORMS_KEY,
    }])

    plugin_result = runner.run()
    if platforms:
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY]
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] == set(result)
    else:
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] is None
