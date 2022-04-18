"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os.path
import time
import inspect
import sys
from typing import Any, Dict, List, Final

from flexmock import flexmock
import pytest

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import (
    Plugin,
    PluginExecutionInfo,
    PluginFailedException,
    PluginsRunner,
    PreBuildSleepPlugin,
)
from atomic_reactor.plugins.add_filesystem import AddFilesystemPlugin
from atomic_reactor.plugins.tag_and_push import TagAndPushPlugin

from tests.constants import DOCKERFILE_GIT

THIS_FILE: Final[str] = os.path.abspath(__file__)
TEST_IMAGE = "fedora:latest"
SOURCE = {"provider": "git", "uri": DOCKERFILE_GIT}
DUMMY_BUILD_RESULT = {"image_id": "image_id"}

pytestmark = pytest.mark.usefixtures('user_params')


class PushImagePlugin(Plugin):
    key = 'push_image'

    def run(self):
        return "pushed"


class CleanupPlugin(Plugin):
    key = 'clean_up'

    def run(self): pass


class StoreArtifactsPlugin(Plugin):
    is_allowed_to_fail = False
    key = 'store_artifacts'

    def run(self):
        raise IOError("no permission")


class WriteRemoteLogsPlugin(Plugin):
    key = "write_logs"

    def run(self):
        raise IOError("remote host is unavailable.")


def teardown_function(function):
    module_name, _, _ = os.path.basename(__file__).partition(".")
    if module_name in sys.modules:
        del sys.modules[module_name]


@pytest.mark.parametrize("use_plugin_file", [True, False])
def test_load_plugins(use_plugin_file, workflow):
    """
    test loading plugins
    """
    plugins_files = [inspect.getfile(PushImagePlugin)] if use_plugin_file else []
    runner = PluginsRunner(workflow, [], plugin_files=plugins_files)

    assert runner.plugin_classes is not None
    assert len(runner.plugin_classes) > 0

    # Randomly verify the plugin existence
    assert AddFilesystemPlugin.key in runner.plugin_classes
    assert TagAndPushPlugin.key in runner.plugin_classes

    if use_plugin_file:
        assert PushImagePlugin.key in runner.plugin_classes
        assert CleanupPlugin.key in runner.plugin_classes


@pytest.mark.parametrize("plugins_conf,expected", [
    [[{"name": "cool_plugin", "required": False}], []],
    [
        [{"name": "cool_plugin"}],
        pytest.raises(PluginFailedException, match="no such plugin"),
    ],
    [
        [{"name": "cool_plugin", "required": True}],
        pytest.raises(PluginFailedException, match="no such plugin"),
    ],
    [
        [
            {"name": PushImagePlugin.key},
            {"name": AddFilesystemPlugin.key, "args": {"arg1": "value"}},
        ],
        [
            PluginExecutionInfo(plugin_name=PushImagePlugin.key,
                                plugin_class=PushImagePlugin,
                                conf={},
                                is_allowed_to_fail=True),
            PluginExecutionInfo(plugin_name=AddFilesystemPlugin.key,
                                plugin_class=AddFilesystemPlugin,
                                conf={"arg1": "value"},
                                is_allowed_to_fail=False),
        ],
    ],
])
def test_get_available_plugins(plugins_conf: List[Dict[str, Any]],
                               expected,
                               workflow: DockerBuildWorkflow):
    if isinstance(expected, list):
        runner = PluginsRunner(
            workflow, plugins_conf,
            plugin_files=[inspect.getfile(PushImagePlugin)],
        )
        expected_exec_info: PluginExecutionInfo
        for got_exec_info, expected_exec_info in zip(runner.available_plugins, expected):
            assert got_exec_info.plugin_name == expected_exec_info.plugin_name
            assert got_exec_info.conf == expected_exec_info.conf
            assert got_exec_info.is_allowed_to_fail == expected_exec_info.is_allowed_to_fail

            # For easy comparison. Otherwise, the different path within class
            # repr has to be handled. For example:
            # tests.test_plugin.MyPlugin1 and test_plugin.MyPlugin1
            left = got_exec_info.plugin_class.__name__.split(".")[-1]
            right = expected_exec_info.plugin_class.__name__.split(".")[-1]
            assert left == right
    else:
        with expected:
            PluginsRunner(workflow, plugins_conf)
        err_msg = workflow.data.plugins_errors["cool_plugin"]
        assert "no such plugin" in err_msg


def test_check_no_reload(workflow):
    """
    test if plugins are not reloaded
    """
    PluginsRunner(workflow,
                  [{"name": PushImagePlugin.key}],
                  plugin_files=[THIS_FILE])
    module_id_first = id(sys.modules['test_plugin'])
    PluginsRunner(workflow,
                  [{"name": PushImagePlugin.key}],
                  plugin_files=[THIS_FILE])
    module_id_second = id(sys.modules['test_plugin'])
    assert module_id_first == module_id_second


@pytest.mark.parametrize('params', [
    {'spam': 'maps'},
    {'spam': 'maps', 'eggs': 'sgge'},
])
def test_runner_create_instance_from_plugin(tmpdir, params):
    workflow = flexmock(data=flexmock())
    workflow.data.image_id = 'image-id'
    workflow.source = flexmock()
    workflow.source.dockerfile_path = 'dockerfile-path'
    workflow.source.path = 'path'
    workflow.user_params = {'shrubbery': 'yrebburhs'}

    class MyPlugin(Plugin):

        key = 'my_plugin'

        @staticmethod
        def args_from_user_params(user_params):
            return {'shrubbery': user_params['shrubbery']}

        def __init__(self, workflow, spam=None, shrubbery=None):
            super().__init__(workflow)
            self.spam = spam
            self.shrubbery = shrubbery

        def run(self):
            pass

    bpr = PluginsRunner(workflow, [])
    plugin = bpr.create_instance_from_plugin(MyPlugin, params)

    assert plugin.spam == params['spam']
    assert plugin.shrubbery == 'yrebburhs'


@pytest.mark.parametrize('params', [
    {'spam': 'maps'},
    {'spam': 'maps', 'eggs': 'sgge'},
])
def test_runner_create_instance_from_plugin_with_kwargs(tmpdir, params):
    workflow = flexmock(data=flexmock())
    workflow.data.image_id = 'image-id'
    workflow.source = flexmock()
    workflow.source.dockerfile_path = 'dockerfile-path'
    workflow.source.path = 'path'
    workflow.user_params = {}

    class MyPlugin(Plugin):

        key = 'my_plugin'

        def __init__(self, workflow, spam=None, **kwargs):
            super().__init__(workflow)
            self.spam = spam
            for key, value in kwargs.items():
                setattr(self, key, value)

        def run(self):
            pass

    bpr = PluginsRunner(workflow, [])
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


def test_store_plugin_result(workflow: DockerBuildWorkflow):
    runner = PluginsRunner(
        workflow,
        [{"name": CleanupPlugin.key}, {"name": PushImagePlugin.key}],
        plugin_files=[THIS_FILE],
    )
    runner.run()

    assert runner.plugins_results[CleanupPlugin.key] is None
    assert "pushed" == runner.plugins_results[PushImagePlugin.key]


@pytest.mark.parametrize("allow_plugin_fail", [True, False])
def test_run_plugins_in_keep_going_mode(
        allow_plugin_fail: bool, workflow: DockerBuildWorkflow, caplog
):
    plugins_conf = [{"name": CleanupPlugin.key}]
    if allow_plugin_fail:
        plugins_conf.append({"name": WriteRemoteLogsPlugin.key})
    else:
        plugins_conf.append({"name": StoreArtifactsPlugin.key})

    # Let the failure happens firstly
    plugins_conf.reverse()

    runner = PluginsRunner(
        workflow, plugins_conf, plugin_files=[THIS_FILE], keep_going=True
    )

    if allow_plugin_fail:
        runner.run()
        # The error should just be logged
        assert "remote host is unavailable" in caplog.text
    else:
        with pytest.raises(PluginFailedException, match="no permission"):
            runner.run()
        # The error must be recorded
        assert "no permission" in workflow.data.plugins_errors[StoreArtifactsPlugin.key]

    # The subsequent plug should get a chance to run after previous error.
    assert "continuing..." in caplog.text
    assert runner.plugins_results[CleanupPlugin.key] is None
