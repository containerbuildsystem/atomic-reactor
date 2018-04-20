"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals
import os
import yaml

from atomic_reactor.constants import PLUGIN_CHECK_AND_SET_PLATFORMS_KEY, REPO_CONTAINER_CONFIG
from atomic_reactor.plugin import PluginFailedException
import atomic_reactor.plugins.pre_reactor_config as reactor_config
from atomic_reactor.plugins.pre_check_and_set_platforms import MustBuildForAmd64
import atomic_reactor.koji_util as koji_util
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.util import ImageName
from flexmock import flexmock
import pytest
from tests.constants import SOURCE, MOCK
from tests.fixtures import reactor_config_map  # noqa:F401
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

    def get_build_file_path(self):
        return self.path, self.path


def prepare(tmpdir):
    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    setattr(workflow, 'builder', X())
    source = MockSource(tmpdir)

    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='21'))
    setattr(workflow.builder, 'source', source)
    setattr(workflow, 'source', source)

    return tasker, workflow


@pytest.mark.parametrize(('platforms',  # noqa:F811
                          'platform_exclude',
                          'platform_only',
                          'result',
                          'valid'), [
    (None, '', 'ppc64le', None, True),
    ('x86_64 ppc64le', '', 'ppc64le', ['ppc64le'], False),
    ('x86_64 spam bacon toast ppc64le', ['spam', 'bacon', 'eggs', 'toast'], '',
     ['x86_64', 'ppc64le'], True),
    ('ppc64le spam bacon toast', ['spam', 'bacon', 'eggs', 'toast'], 'ppc64le',
     ['ppc64le'], False),
    ('x86_64 bacon toast', 'toast', ['x86_64', 'ppc64le'], ['x86_64'], True),
    ('x86_64 toast', 'toast', 'x86_64', ['x86_64'], True),
    ('x86_64 spam bacon toast', ['spam', 'bacon', 'eggs', 'toast'],
     ['x86_64', 'ppc64le'], ['x86_64'], True),
    ('x86_64 ppc64le', '', '', ['x86_64', 'ppc64le'], True)
])
def test_check_and_set_platforms(tmpdir, platforms, platform_exclude,
                                 platform_only, result, valid,
                                 reactor_config_map):
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

    tasker, workflow = prepare(tmpdir)

    session = mock_session(platforms)
    mock_koji_config = {
        'auth': {},
        'hub_url': 'test',
    }
    flexmock(reactor_config).should_receive('get_koji').and_return(mock_koji_config)
    flexmock(koji_util).should_receive('create_koji_session').and_return(session)

    if reactor_config_map:
        reactor_map = {
            'version': 1,
            'platform_descriptors': [{
                'platform': 'x86_64',
                'architecture': 'amd64',
            }],
        }
        workspace_key = reactor_config.ReactorConfigPlugin.key
        conf_key = reactor_config.WORKSPACE_CONF_KEY
        workflow.plugin_workspace[workspace_key] = {
            conf_key: reactor_config.ReactorConfig(reactor_map),
        }
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': PLUGIN_CHECK_AND_SET_PLATFORMS_KEY,
        'args': {
            'koji_target': KOJI_TARGET,
        },
    }])

    if reactor_config_map and not valid:
        with pytest.raises(PluginFailedException) as exc:
            runner.run()
            assert exc.args[0] == MustBuildForAmd64
        return

    plugin_result = runner.run()
    if platforms:
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY]
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] == set(result)
    else:
        assert plugin_result[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] is None
