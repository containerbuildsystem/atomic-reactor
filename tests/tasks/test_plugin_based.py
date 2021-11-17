"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from flexmock import flexmock
import pytest

from osbs.exceptions import OsbsValidationException

from atomic_reactor import inner
from atomic_reactor.tasks import plugin_based


class TestPluginsDef:
    """Tests for the PluginsDef class."""

    def test_create_valid(self):
        plugins = plugin_based.PluginsDef(buildstep=[{"name": "some_plugin"}])
        assert plugins.prebuild == []
        assert plugins.buildstep == [{"name": "some_plugin"}]
        assert plugins.prepublish == []
        assert plugins.postbuild == []
        assert plugins.exit == []

    def test_create_invalid(self):
        with pytest.raises(OsbsValidationException, match="1 is not of type 'boolean'"):
            plugin_based.PluginsDef(prebuild=[{"name": "some_plugin", "required": 1}])


class TestPluginBasedTask:
    """Tests for the PluginBasedTask class"""

    @pytest.fixture
    def task_with_mocked_deps(self, monkeypatch, build_dir):
        """Create a PluginBasedTask instance with mocked task parameters.

        Mock DockerBuildWorkflow accordingly. Return the mocked workflow instance for further
        customization in individual tests.
        """
        expect_source = flexmock()
        task_params = flexmock(build_dir=build_dir,
                               source=expect_source,
                               user_params={"a": "b"},
                               config_file="config.yaml")

        expect_plugins = flexmock()
        monkeypatch.setattr(plugin_based.PluginBasedTask, "plugins_def", expect_plugins)

        mocked_workflow = flexmock(inner.DockerBuildWorkflow)
        (
            mocked_workflow
            .should_receive("__init__")
            .once()
            .with_args(
                build_dir,
                source=expect_source,
                plugins=expect_plugins,
                user_params={"a": "b"},
                reactor_config_path="config.yaml",
            )
        )
        mocked_workflow.should_receive("build_docker_image").and_raise(
            AssertionError("you must mock the build_docker_image() workflow method")
        )

        task = plugin_based.PluginBasedTask(task_params)
        return task, mocked_workflow

    def test_execute(self, task_with_mocked_deps, caplog):
        task, mocked_workflow = task_with_mocked_deps

        success = inner.BuildResult()
        mocked_workflow.should_receive("build_docker_image").and_return(success)

        task.execute()
        assert r"task finished successfully \o/" in caplog.text

    def test_execute_raises_exception(self, task_with_mocked_deps, caplog):
        task, mocked_workflow = task_with_mocked_deps

        error = ValueError("something went wrong")
        mocked_workflow.should_receive("build_docker_image").and_raise(error)

        with pytest.raises(ValueError, match="something went wrong"):
            task.execute()

        assert "task failed: something went wrong" in caplog.text

    def test_execute_returns_failure(self, task_with_mocked_deps, caplog):
        task, mocked_workflow = task_with_mocked_deps

        failure = inner.BuildResult(fail_reason="workflow returned failure")
        mocked_workflow.should_receive("build_docker_image").and_return(failure)

        with pytest.raises(RuntimeError, match="task failed: workflow returned failure"):
            task.execute()

        assert "task failed: workflow returned failure" in caplog.text
