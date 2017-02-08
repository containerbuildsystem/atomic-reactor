"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import json
import os

from dockerfile_parse import DockerfileParser
from flexmock import flexmock
import pytest

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import (BuildPluginsRunner, PreBuildPluginsRunner,
                                   PostBuildPluginsRunner, InputPluginsRunner,
                                   PluginFailedException, PrePublishPluginsRunner,
                                   ExitPluginsRunner, BuildStepPluginRunner,
                                   PluginsRunner, InappropriateBuildStepError,
                                   BuildStepPlugin)
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin
from atomic_reactor.plugins.pre_add_yum_repo_by_url import AddYumRepoByUrlPlugin
from atomic_reactor.plugins.build_docker_api import DockerApiPlugin
from atomic_reactor.util import ImageName, df_parser

from tests.fixtures import docker_tasker
from tests.constants import DOCKERFILE_GIT, MOCK
if MOCK:
    from tests.docker_mock import mock_docker


TEST_IMAGE = "fedora:latest"
SOURCE = {"provider": "git", "uri": DOCKERFILE_GIT}

def mock_workflow(tmpdir):
    if MOCK:
        mock_docker()

    workflow = DockerBuildWorkflow(SOURCE, 'test-image')
    setattr(workflow, 'builder', X())
    flexmock(DockerfileParser, content='df_content')
    setattr(workflow.builder, 'get_built_image_info', flexmock())
    flexmock(workflow.builder, get_built_image_info={'Id': 'some'})
    setattr(workflow.builder, '_ensure_not_built', flexmock())
    flexmock(workflow.builder, _ensure_not_built=None)
    setattr(workflow.builder, 'image_id', 'image-id')
    setattr(workflow.builder, 'source', flexmock())
    setattr(workflow.builder, 'df_path', 'df_path')
    setattr(workflow.builder.source, 'dockerfile_path', 'dockerfile-path')
    setattr(workflow.builder, 'image', flexmock())
    setattr(workflow.builder.image, 'to_str', lambda: 'image')
    setattr(workflow.builder.source, 'path', 'path')
    setattr(workflow.builder, 'base_image', flexmock())
    setattr(workflow.builder.base_image, 'to_str', lambda: 'base-image')

    return workflow

@pytest.mark.parametrize('runner_type', [
    PreBuildPluginsRunner,
    PrePublishPluginsRunner,
    PostBuildPluginsRunner,
    ExitPluginsRunner,
    BuildStepPluginRunner,
])
def test_load_plugins(docker_tasker, runner_type):
    """
    test loading plugins
    """
    runner = runner_type(docker_tasker, DockerBuildWorkflow(SOURCE, ""), None)
    assert runner.plugin_classes is not None
    assert len(runner.plugin_classes) > 0


class X(object):
    pass


def test_prebuild_plugin_failure(docker_tasker):
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    assert workflow.build_process_failed is False
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'base_image', ImageName(repo='fedora', tag='21'))
    setattr(workflow.builder, "source", X())
    setattr(workflow.builder.source, 'dockerfile_path', "/non/existent")
    setattr(workflow.builder.source, 'path', "/non/existent")
    runner = PreBuildPluginsRunner(docker_tasker, workflow,
                                   [{"name": AddYumRepoByUrlPlugin.key,
                                     "args": {'repourls': True}}])
    with pytest.raises(PluginFailedException):
        results = runner.run()
    assert workflow.build_process_failed is True


@pytest.mark.parametrize('runner_type', [
    PreBuildPluginsRunner,
    PrePublishPluginsRunner,
    PostBuildPluginsRunner,
    ExitPluginsRunner,
    BuildStepPluginRunner,
])
@pytest.mark.parametrize('required', [
    True,
    False,
])
def test_required_plugin_failure(tmpdir, docker_tasker, runner_type, required):
    """
    test required option for plugins
    and check if it fails when is required
    and also check plugin_failed value
    """
    workflow = mock_workflow(tmpdir)
    assert workflow.plugin_failed is False
    runner = runner_type(docker_tasker, workflow,
                         [{"name": "no_such_plugin",
                           "required": required}])
    if required:
        with pytest.raises(PluginFailedException):
            results = runner.run()
    else:
        results = runner.run()
    assert workflow.plugin_failed is required

@pytest.mark.parametrize('success1,success2', [
    (True, True),
    (True, False),
    (False, True),
    (False, False),
])
@pytest.mark.parametrize('stop', [
    True,
    False,
])
def test_stop_on_success_build_plugin(caplog, tmpdir, docker_tasker, stop, success1, success2):
    """
    test stop on success option
    if 'stop' is true, plugin runner should stop after first successful plugin
    and if 'stop' is false it should run all plugins
    InappropriateBuildStepError exception isn't critical,
    and won't fail buildstep runner
    """
    workflow = mock_workflow(tmpdir)
    flexmock(PluginsRunner, load_plugins=lambda x: {
                                        DockerApiPlugin.key: DockerApiPlugin,
                                        MyPlugin.key: MyPlugin})

    class MyPlugin(BuildStepPlugin):
        key = 'MyPlugin'

    runner = BuildStepPluginRunner(docker_tasker, workflow,
                                   [{"name": DockerApiPlugin.key},
                                    {"name": MyPlugin.key}])

    if stop:
        if success1:
            flexmock(DockerApiPlugin).should_receive('run')
            flexmock(MyPlugin).should_receive('run').times(0)
        else:
            flexmock(DockerApiPlugin).should_receive('run').and_raise(InappropriateBuildStepError).times(1)
            flexmock(MyPlugin).should_receive('run').times(1)
    else:
        if success1:
            flexmock(DockerApiPlugin).should_receive('run').times(1)
        else:
            flexmock(DockerApiPlugin).should_receive('run').and_raise(InappropriateBuildStepError).times(1)
        if success2:
            flexmock(MyPlugin).should_receive('run').times(1)
        else:
            flexmock(MyPlugin).should_receive('run').and_raise(InappropriateBuildStepError).times(1)

    results = runner.run(stop_on_success=stop)

    if stop:
        expected_log_message = "stopping further execution of plugins after first successful plugin"
        assert expected_log_message in [l.getMessage() for l in caplog.records()]

@pytest.mark.parametrize('success1,success2', [
    (True, True),
    (True, False),
    (False, True),
    (False, False),
])
@pytest.mark.parametrize('stop', [
    True,
    False,
])
def test_stop_on_success_build_plugin_failing_exception(tmpdir, caplog, docker_tasker, stop, success1, success2):
    """
    test stop on success option
    if 'stop' is true, plugin runner should stop after first successful plugin
    and if 'stop' is false it should run all plugins
    Exception exception is critical,
    and will fail buildstep runner
    """
    workflow = mock_workflow(tmpdir)
    flexmock(PluginsRunner, load_plugins=lambda x: {
                                        DockerApiPlugin.key: DockerApiPlugin,
                                        MyPlugin.key: MyPlugin})

    class MyPlugin(BuildStepPlugin):
        key = 'MyPlugin'

    runner = BuildStepPluginRunner(docker_tasker, workflow,
                                   [{"name": DockerApiPlugin.key},
                                    {"name": MyPlugin.key}])

    will_fail = False
    if stop:
        if success1:
            flexmock(DockerApiPlugin).should_receive('run')
            flexmock(MyPlugin).should_receive('run').times(0)
        else:
            flexmock(DockerApiPlugin).should_receive('run').and_raise(Exception).times(1)
            will_fail = True
            flexmock(MyPlugin).should_receive('run').times(0)
    else:
        if success1:
            flexmock(DockerApiPlugin).should_receive('run').times(1)
            if success2:
                flexmock(MyPlugin).should_receive('run').times(1)
            else:
                flexmock(MyPlugin).should_receive('run').and_raise(InappropriateBuildStepError).times(1)

        else:
            flexmock(DockerApiPlugin).should_receive('run').and_raise(Exception).times(1)
            will_fail = True
            flexmock(MyPlugin).should_receive('run').times(0)

    if will_fail:
        with pytest.raises(Exception):
            runner.run(stop_on_success=stop)

    else:
        runner.run(stop_on_success=stop)
        if stop:
            expected_log_message = "stopping further execution of plugins after first successful plugin"
            assert expected_log_message in [l.getMessage() for l in caplog.records()]


def test_fallback_to_docker_build(docker_tasker):
    """
    test fallback to docker build
    if no build plugins specified
    docker build plugin should be added and run
    """
    workflow = DockerBuildWorkflow(SOURCE, 'test-image')
    setattr(workflow, 'builder', X())

    runner = BuildStepPluginRunner(docker_tasker, workflow, [])
    assert runner.plugins_conf == [{'name': 'docker_api'}]


class TestBuildPluginsRunner(object):

    @pytest.mark.parametrize(('params'), [
        {'spam': 'maps'},
        {'spam': 'maps', 'eggs': 'sgge'},
    ])
    def test_create_instance_from_plugin(self, tmpdir, params):
        workflow = flexmock()
        workflow.builder = flexmock()
        workflow.builder.image_id = 'image-id'
        workflow.builder.source = flexmock()
        workflow.builder.source.dockerfile_path = 'dockerfile-path'
        workflow.builder.source.path = 'path'
        workflow.builder.base_image = flexmock()
        workflow.builder.base_image.to_str = lambda: 'base-image'

        tasker = flexmock()

        class MyPlugin(object):
            def __init__(self, tasker, workflow, spam=None):
                self.spam = spam

        bpr = BuildPluginsRunner(tasker, workflow, 'PreBuildPlugin', {})
        plugin = bpr.create_instance_from_plugin(MyPlugin, params)

        assert plugin.spam == params['spam']

    @pytest.mark.parametrize(('params'), [
        {'spam': 'maps'},
        {'spam': 'maps', 'eggs': 'sgge'},
    ])
    def test_create_instance_from_plugin_with_kwargs(self, tmpdir, params):
        workflow = flexmock()
        workflow.builder = flexmock()
        workflow.builder.image_id = 'image-id'
        workflow.builder.source = flexmock()
        workflow.builder.source.dockerfile_path = 'dockerfile-path'
        workflow.builder.source.path = 'path'
        workflow.builder.base_image = flexmock()
        workflow.builder.base_image.to_str = lambda: 'base-image'

        tasker = flexmock()

        class MyPlugin(object):
            def __init__(self, tasker, workflow, spam=None, **kwargs):
                self.spam = spam
                for key, value in kwargs.items():
                    setattr(self, key, value)

        bpr = BuildPluginsRunner(tasker, workflow, 'PreBuildPlugin', {})
        plugin = bpr.create_instance_from_plugin(MyPlugin, params)

        for key, value in params.items():
            assert getattr(plugin, key) == value


class TestInputPluginsRunner(object):
    def test_substitution(self, tmpdir):
        tmpdir_path = str(tmpdir)
        build_json_path = os.path.join(tmpdir_path, "build.json")
        with open(build_json_path, 'w') as fp:
            json.dump({
                "image": "some-image"
            }, fp)
        changed_image_name = "changed-image-name"
        runner = InputPluginsRunner([{"name": "path",
                                      "args": {
                                          "path": build_json_path,
                                          "substitutions": {
                                              "image": changed_image_name
        }}}])
        results = runner.run()
        assert results['path']['image'] == changed_image_name


    def test_substitution_on_plugins(self, tmpdir):
        tmpdir_path = str(tmpdir)
        build_json_path = os.path.join(tmpdir_path, "build.json")
        with open(build_json_path, 'w') as fp:
            json.dump({
                "image": "some-image",
                "prebuild_plugins": [{
                    'name': 'asd',
                    'args': {
                        'key': 'value1'
                    }
                }]
            }, fp)
        changed_value = "value-123"
        runner = InputPluginsRunner([{"name": "path",
                                      "args": {"path": build_json_path,
                                               "substitutions": {
                                                   "prebuild_plugins.asd.key": changed_value}}}])
        results = runner.run()
        assert results['path']['prebuild_plugins'][0]['args']['key'] == changed_value

    def test_autoinput_no_autousable(self):
        flexmock(os, environ={})
        runner = InputPluginsRunner([{'name': 'auto', 'args': {}}])
        with pytest.raises(PluginFailedException) as e:
            runner.run()
        assert 'No autousable input plugin' in str(e)

    def test_autoinput_more_autousable(self):
        # mock env vars checked by both env and osv3 input plugins
        flexmock(os, environ={'BUILD': 'a', 'SOURCE_URI': 'b', 'OUTPUT_IMAGE': 'c', 'BUILD_JSON': 'd'})
        runner = InputPluginsRunner([{'name': 'auto', 'args': {}}])
        with pytest.raises(PluginFailedException) as e:
            runner.run()
        assert 'More than one usable plugin with "auto" input' in str(e)
        assert 'osv3, env' in str(e) or 'env, osv3' in str(e)

    def test_autoinput_one_autousable(self):
        # mock env var for env input plugin
        flexmock(os, environ={'BUILD_JSON': json.dumps({'image': 'some-image'})})
        runner = InputPluginsRunner([{'name': 'auto', 'args': {'substitutions': {}}}])
        results = runner.run()
        assert results == {'auto': {'image': 'some-image'}}
