"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import time
import inspect

from flexmock import flexmock
import pytest

from atomic_reactor.inner import BuildResult
from atomic_reactor.plugin import (BuildPluginsRunner, PreBuildPluginsRunner,
                                   PostBuildPluginsRunner,
                                   PluginFailedException, PrePublishPluginsRunner,
                                   ExitPluginsRunner, BuildStepPluginsRunner,
                                   PluginsRunner, InappropriateBuildStepError,
                                   BuildPlugin, BuildStepPlugin, PreBuildPlugin, ExitPlugin,
                                   PreBuildSleepPlugin, PrePublishPlugin, PostBuildPlugin)

from tests.constants import DOCKERFILE_GIT

TEST_IMAGE = "fedora:latest"
SOURCE = {"provider": "git", "uri": DOCKERFILE_GIT}
DUMMY_BUILD_RESULT = BuildResult(image_id="image_id")

pytestmark = pytest.mark.usefixtures('user_params')


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


@pytest.mark.parametrize('runner_type', [  # noqa
    PreBuildPluginsRunner,
    PrePublishPluginsRunner,
    PostBuildPluginsRunner,
    ExitPluginsRunner,
    BuildStepPluginsRunner,
])
def test_load_plugins(runner_type, workflow):
    """
    test loading plugins
    """
    runner = runner_type(workflow, None)
    assert runner.plugin_classes is not None
    assert len(runner.plugin_classes) > 0


class X(object):
    pass


@pytest.mark.parametrize('runner_type', [  # noqa
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
def test_required_plugin_failure(workflow, runner_type, required):
    """
    test required option for plugins
    and check if it fails when is required
    and also check plugin_failed value
    """
    assert workflow.plugin_failed is False
    params = (workflow,
              [{"name": "no_such_plugin",
               "required": required}])

    if required or runner_type == BuildStepPluginsRunner:
        with pytest.raises(PluginFailedException):
            runner = runner_type(*params)
            runner.run()
    else:
        runner = runner_type(*params)
        runner.run()
    if runner_type == BuildStepPluginsRunner:
        assert workflow.plugin_failed is True
    else:
        assert workflow.plugin_failed is required


@pytest.mark.parametrize('runner_type, plugin_type', [  # noqa
    (PreBuildPluginsRunner, PreBuildPlugin),
    (PrePublishPluginsRunner, PrePublishPlugin),
    (PostBuildPluginsRunner, PostBuildPlugin),
    (ExitPluginsRunner, ExitPlugin),
    (BuildStepPluginsRunner, BuildStepPlugin),
])
@pytest.mark.parametrize('required', [
    True,
    False,
])
def test_verify_required_plugins_before_first_run(caplog, workflow, runner_type, plugin_type,
                                                  required):
    """
    test plugin availability checks before running any plugins
    """
    class MyPlugin(plugin_type):
        key = 'MyPlugin'

        def run(self):
            return DUMMY_BUILD_RESULT

    flexmock(PluginsRunner, load_plugins=lambda x: {
                                        MyPlugin.key: MyPlugin})
    params = (workflow,
              [{"name": MyPlugin.key, "required": False},
               {"name": "no_such_plugin", "required": required}])
    expected_log_message = "running plugin '%s'" % MyPlugin.key

    # build step plugins set "required" to False
    if required and (runner_type != BuildStepPluginsRunner):
        with pytest.raises(PluginFailedException):
            runner = runner_type(*params)
            runner.run()
        assert all(expected_log_message not in log.getMessage() for log in caplog.records)
    else:
        runner = runner_type(*params)
        runner.run()
        assert any(expected_log_message in log.getMessage() for log in caplog.records)


def test_check_no_reload(caplog, workflow):
    """
    test if plugins are not reloaded
    """
    this_file = inspect.getfile(MyBsPlugin1)
    expected_log_message = "load file '%s'" % this_file
    BuildStepPluginsRunner(workflow,
                           [{"name": "MyBsPlugin1"}],
                           plugin_files=[this_file])
    assert any(expected_log_message in log.getMessage() for log in caplog.records)
    log_len = len(caplog.records)
    BuildStepPluginsRunner(workflow,
                           [{"name": "MyBsPlugin1"}],
                           plugin_files=[this_file])
    assert all(expected_log_message not in log.getMessage() for log in caplog.records[log_len:])

@pytest.mark.parametrize('success1', [True, False])  # noqa
@pytest.mark.parametrize('success2', [True, False])
def test_buildstep_phase_build_plugin(caplog, workflow, success1, success2):
    """
    plugin runner should stop after first successful plugin
    InappropriateBuildStepError exception isn't critical,
    and won't fail buildstep runner
    unless no plugin finished successfully
    """
    flexmock(PluginsRunner, load_plugins=lambda x: {
                                        MyBsPlugin1.key: MyBsPlugin1,
                                        MyBsPlugin2.key: MyBsPlugin2, })
    runner = BuildStepPluginsRunner(workflow,
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
            flexmock(MyBsPlugin2).should_receive('run').and_raise(InappropriateBuildStepError
                                                                  ).once()
            will_fail = True

    if will_fail:
        with pytest.raises(Exception):
            runner.run()
    else:
        runner.run()
        expected_log_message = "stopping further execution of plugins after first successful plugin"
        assert expected_log_message in [log.getMessage() for log in caplog.records]


@pytest.mark.parametrize('success1', [True, False])  # noqa
def test_buildstep_phase_build_plugin_failing_exception(workflow, caplog, success1):
    """
    plugin runner should stop after first successful plugin
    Exception exception is critical,
    and will fail buildstep runner
    """
    flexmock(PluginsRunner, load_plugins=lambda x: {
                                        MyBsPlugin1.key: MyBsPlugin1,
                                        MyBsPlugin2.key: MyBsPlugin2, })
    runner = BuildStepPluginsRunner(workflow,
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
        assert expected_log_message in [log.getMessage() for log in caplog.records]


def test_non_buildstep_phase_raises_InappropriateBuildStepError(caplog, workflow):  # noqa
    """
    tests that exception is raised if no buildstep_phase
    but raises InappropriateBuildStepError
    """
    flexmock(PluginsRunner, load_plugins=lambda x: {
                                        MyPreBuildPlugin.key: MyPreBuildPlugin})
    runner = PreBuildPluginsRunner(workflow,
                                   [{"name": MyPreBuildPlugin.key}])

    with pytest.raises(Exception):
        runner.run()


def test_no_appropriate_buildstep_build_plugin(caplog, workflow):  # noqa
    """
    test that build fails if there isn't any
    appropriate buildstep plugin (doesn't exist)
    """
    flexmock(PluginsRunner, load_plugins=lambda x: {})
    runner = BuildStepPluginsRunner(workflow,
                                    [{"name": MyBsPlugin1.key},
                                     {"name": MyBsPlugin2.key}])

    with pytest.raises(Exception):
        runner.run()


@pytest.mark.parametrize('pluginconf_method, expected', [  # noqa
    ('orchestrator', 'orchestrator'),
])
def test_which_buildstep_plugin_configured(workflow, pluginconf_method, expected):
    """
    test buildstep plugin adjustments.
    if no/empty build_step specified,
    build plugin from source or default will run
    """
    expected = [{'name': expected, 'is_allowed_to_fail': False}]
    plugins_conf = pluginconf_method
    if pluginconf_method:
        plugins_conf = [{'name': pluginconf_method, 'is_allowed_to_fail': False}]
        expected[0]['required'] = False

    runner = BuildStepPluginsRunner(workflow, plugins_conf)
    assert runner.plugins_conf == expected


class TestBuildPluginsRunner(object):

    @pytest.mark.parametrize(('params'), [
        {'spam': 'maps'},
        {'spam': 'maps', 'eggs': 'sgge'},
    ])
    def test_create_instance_from_plugin(self, tmpdir, params):
        workflow = flexmock()
        workflow.image_id = 'image-id'
        workflow.source = flexmock()
        workflow.source.dockerfile_path = 'dockerfile-path'
        workflow.source.path = 'path'
        workflow.user_params = {'shrubbery': 'yrebburhs'}

        class MyPlugin(BuildPlugin):

            key = 'my_plugin'

            @staticmethod
            def args_from_user_params(user_params):
                return {'shrubbery': user_params['shrubbery']}

            def __init__(self, workflow, spam=None, shrubbery=None):
                self.spam = spam
                self.shrubbery = shrubbery

            def run(self):
                pass

        bpr = BuildPluginsRunner(workflow, 'PreBuildPlugin', {})
        plugin = bpr.create_instance_from_plugin(MyPlugin, params)

        assert plugin.spam == params['spam']
        assert plugin.shrubbery == 'yrebburhs'

    @pytest.mark.parametrize(('params'), [
        {'spam': 'maps'},
        {'spam': 'maps', 'eggs': 'sgge'},
    ])
    def test_create_instance_from_plugin_with_kwargs(self, tmpdir, params):
        workflow = flexmock()
        workflow.image_id = 'image-id'
        workflow.source = flexmock()
        workflow.source.dockerfile_path = 'dockerfile-path'
        workflow.source.path = 'path'
        workflow.user_params = {}

        class MyPlugin(BuildPlugin):

            key = 'my_plugin'

            def __init__(self, workflow, spam=None, **kwargs):
                self.spam = spam
                for key, value in kwargs.items():
                    setattr(self, key, value)

            def run(self):
                pass

        bpr = BuildPluginsRunner(workflow, 'PreBuildPlugin', {})
        plugin = bpr.create_instance_from_plugin(MyPlugin, params)

        for key, value in params.items():
            assert getattr(plugin, key) == value


class TestPreBuildSleepPlugin(object):
    @pytest.mark.parametrize(('seconds', 'exp'), [(None, 60), (1, 1)])
    def test_sleep_plugin(self, seconds, exp):
        (flexmock(time)
         .should_receive('sleep')
         .with_args(exp)
         .once())

        kwargs = {
            'workflow': None,
        }
        if seconds is not None:
            kwargs['seconds'] = seconds

        plugin = PreBuildSleepPlugin(**kwargs)
        plugin.run()
