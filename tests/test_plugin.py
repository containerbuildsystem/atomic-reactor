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
from atomic_reactor.build import BuildResult
from atomic_reactor.plugin import (BuildPluginsRunner, PreBuildPluginsRunner,
                                   PostBuildPluginsRunner, InputPluginsRunner,
                                   PluginFailedException, PrePublishPluginsRunner,
                                   ExitPluginsRunner, BuildStepPluginsRunner,
                                   PluginsRunner, InappropriateBuildStepError,
                                   BuildStepPlugin, PreBuildPlugin)
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
DUMMY_BUILD_RESULT  = BuildResult(image_id="image_id")


class MyBsPlugin1(BuildStepPlugin):
    key = 'MyBsPlugin1'

    def run(self):
        return DUMMY_BUILD_RESULT


class MyBsPlugin2(BuildStepPlugin):
    key = 'MyBsPlugin2'

    def run(self):
        return DUMMY_BUILD_RESULT


class MyPreBuildPlugin(PreBuildPlugin):
    key = 'MyPreBuildPlugin'

    def run(self):
        raise InappropriateBuildStepError

def mock_workflow(tmpdir):
    if MOCK:
        mock_docker()

    workflow = DockerBuildWorkflow(SOURCE, 'test-image')
    setattr(workflow, 'builder', X())
    flexmock(DockerfileParser, content='df_content')
    setattr(workflow.builder, 'get_built_image_info', flexmock())
    flexmock(workflow.builder, get_built_image_info={'Id': 'some'})
    setattr(workflow.builder, 'ensure_not_built', flexmock())
    flexmock(workflow.builder, ensure_not_built=None)
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
    BuildStepPluginsRunner,
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
    BuildStepPluginsRunner,
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

    if required or runner_type == BuildStepPluginsRunner:
        with pytest.raises(PluginFailedException):
            results = runner.run()
    else:
        results = runner.run()
    if runner_type == BuildStepPluginsRunner:
        assert workflow.plugin_failed is True
    else:
        assert workflow.plugin_failed is required

@pytest.mark.parametrize('success1', [True, False])
@pytest.mark.parametrize('success2', [True, False])
def test_buildstep_phase_build_plugin(caplog, tmpdir, docker_tasker, success1, success2):
    """
    plugin runner should stop after first successful plugin
    InappropriateBuildStepError exception isn't critical,
    and won't fail buildstep runner
    unless no plugin finished successfully
    """
    workflow = mock_workflow(tmpdir)
    flexmock(PluginsRunner, load_plugins=lambda x: {
                                        MyBsPlugin1.key: MyBsPlugin1,
                                        MyBsPlugin2.key: MyBsPlugin2,})
    runner = BuildStepPluginsRunner(docker_tasker, workflow,
                                   [{"name": MyBsPlugin1.key},
                                    {"name": MyBsPlugin2.key}])

    will_fail = False
    if success1:
        flexmock(MyBsPlugin1).should_call('run').once()
        flexmock(MyBsPlugin2).should_call('run').never()
    else:
        flexmock(MyBsPlugin1).should_receive('run').and_raise(InappropriateBuildStepError).once()
        if success2:
            flexmock(MyBsPlugin2).should_call('run').once()
        else:
            flexmock(MyBsPlugin2).should_receive('run').and_raise(InappropriateBuildStepError).once()
            will_fail = True

    if will_fail:
        with pytest.raises(Exception):
            runner.run()
    else:
        runner.run()
        expected_log_message = "stopping further execution of plugins after first successful plugin"
        assert expected_log_message in [l.getMessage() for l in caplog.records()]

@pytest.mark.parametrize('success1', [True, False])
def test_buildstep_phase_build_plugin_failing_exception(tmpdir, caplog, docker_tasker, success1):
    """
    plugin runner should stop after first successful plugin
    Exception exception is critical,
    and will fail buildstep runner
    """
    workflow = mock_workflow(tmpdir)
    flexmock(PluginsRunner, load_plugins=lambda x: {
                                        MyBsPlugin1.key: MyBsPlugin1,
                                        MyBsPlugin2.key: MyBsPlugin2,})
    runner = BuildStepPluginsRunner(docker_tasker, workflow,
                                   [{"name": MyBsPlugin1.key},
                                    {"name": MyBsPlugin2.key}])

    will_fail = False
    if success1:
        flexmock(MyBsPlugin1).should_call('run').once()
        flexmock(MyBsPlugin2).should_call('run').never()
    else:
        flexmock(MyBsPlugin1).should_receive('run').and_raise(Exception).once()
        will_fail = True
        flexmock(MyBsPlugin2).should_call('run').never()

    if will_fail:
        with pytest.raises(Exception):
            runner.run()
    else:
        runner.run()
        expected_log_message = "stopping further execution of plugins after first successful plugin"
        assert expected_log_message in [l.getMessage() for l in caplog.records()]


def test_non_buildstep_phase_raises_InappropriateBuildStepError(caplog, tmpdir, docker_tasker):
    """
    tests that exception is raised if no buildstep_phase
    but raises InappropriateBuildStepError
    """
    workflow = mock_workflow(tmpdir)
    flexmock(PluginsRunner, load_plugins=lambda x: {
                                        MyPreBuildPlugin.key: MyPreBuildPlugin})
    runner = PreBuildPluginsRunner(docker_tasker, workflow,
                                   [{"name": MyPreBuildPlugin.key}])

#    flexmock(MyBsPlugin1).should_call('run').and_raise(InappropriateBuildStepError).once()
    with pytest.raises(Exception):
        runner.run()


def test_no_appropriate_buildstep_build_plugin(caplog, tmpdir, docker_tasker):
    """
    test that build fails if there isn't any
    appropriate buildstep plugin (doesn't exist)
    """
    workflow = mock_workflow(tmpdir)
    flexmock(PluginsRunner, load_plugins=lambda x: {})
    runner = BuildStepPluginsRunner(docker_tasker, workflow,
                                   [{"name": MyBsPlugin1.key},
                                    {"name": MyBsPlugin2.key}])

    with pytest.raises(Exception):
        runner.run()

def test_fallback_to_docker_build(docker_tasker):
    """
    test fallback to docker build
    if no build_step specified or
    if empty list of build step plugins specified
    docker build plugin will run
    """
    workflow = DockerBuildWorkflow(SOURCE, 'test-image')
    setattr(workflow, 'builder', X())

    runner = BuildStepPluginsRunner(docker_tasker, workflow, [])
    assert runner.plugins_conf == []

    runner = BuildStepPluginsRunner(docker_tasker, workflow, None)
    assert runner.plugins_conf == [{'name': 'docker_api', 'is_allowed_to_fail': False}]


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
