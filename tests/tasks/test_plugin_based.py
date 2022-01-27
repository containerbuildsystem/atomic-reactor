"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import multiprocessing
import signal

import time
from pathlib import Path

from flexmock import flexmock
import pytest

from atomic_reactor.inner import ImageBuildWorkflowData, BuildResult
from atomic_reactor.plugin import BuildCanceledException
from atomic_reactor.source import DummySource
from atomic_reactor.tasks.common import TaskParams
from atomic_reactor.util import DockerfileImages
from osbs.exceptions import OsbsValidationException

from atomic_reactor import inner
from atomic_reactor.dirs import RootBuildDir, ContextDir
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


def fake_build_docker_image_normal_return(*args, **kwargs) -> BuildResult:
    return BuildResult(logs=["Build successfully."])


class TestPluginBasedTask:
    """Tests for the PluginBasedTask class"""

    @pytest.fixture
    def task_with_mocked_deps(self, monkeypatch, build_dir, tmpdir):
        """Create a PluginBasedTask instance with mocked task parameters.

        Mock DockerBuildWorkflow accordingly. Return the mocked workflow instance for further
        customization in individual tests.
        """
        context_dir = tmpdir.join("context_dir")
        expect_source = flexmock()
        task_params = flexmock(build_dir=build_dir,
                               context_dir=str(context_dir),
                               source=expect_source,
                               user_params={"a": "b"},
                               config_file="config.yaml")

        expect_plugins = flexmock()
        monkeypatch.setattr(plugin_based.PluginBasedTask, "plugins_def", expect_plugins)

        root_build_dir = RootBuildDir(build_dir)

        # Help to verify the RootBuildDir object is passed to the workflow object.
        (flexmock(plugin_based.PluginBasedTask)
         .should_receive("_get_build_dir")
         .and_return(root_build_dir))

        wf_data = ImageBuildWorkflowData()
        flexmock(plugin_based.ImageBuildWorkflowData).new_instances(wf_data)

        mocked_workflow = flexmock(inner.DockerBuildWorkflow)
        (
            mocked_workflow
            .should_receive("__init__")
            .once()
            .with_args(
                root_build_dir,
                wf_data,
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

    @pytest.mark.parametrize(
        "build_result",
        ["normal_return", "error_raised", "failed", "terminated"]
    )
    def test_ensure_workflow_data_is_saved_in_various_conditions(
        self, build_result, build_dir, tmpdir
    ):
        context_dir = tmpdir.join("context_dir").mkdir()
        params = TaskParams(build_dir=str(build_dir),
                            context_dir=str(context_dir),
                            config_file="config.yaml",
                            user_params={})
        (flexmock(params)
         .should_receive("source")
         .and_return(DummySource("git", "https://git.host/")))

        task = plugin_based.PluginBasedTask(params)

        if build_result == "normal_return":
            (flexmock(plugin_based.inner.DockerBuildWorkflow)
             .should_receive("build_docker_image")
             .and_return(BuildResult(logs=["Build successfully."])))

            task.execute()

        elif build_result == "error_raised":
            (flexmock(plugin_based.inner.DockerBuildWorkflow)
             .should_receive("build_docker_image")
             .and_raise(BuildCanceledException))

            with pytest.raises(BuildCanceledException):
                task.execute()

        elif build_result == "failed":
            (flexmock(plugin_based.inner.DockerBuildWorkflow)
             .should_receive("build_docker_image")
             .and_return(BuildResult(fail_reason="Missing Dockerfile")))

            with pytest.raises(RuntimeError, match="task failed: Missing Dockerfile"):
                task.execute()

        elif build_result == "terminated":
            # Start the task.execute in a separate process and terminate it.
            # This simulates the Cancel behavior by TERM signal.

            def _build_docker_image(self, *args, **kwargs):

                def _cancel_build(*args, **kwargs):
                    raise BuildCanceledException()

                signal.signal(signal.SIGTERM, _cancel_build)
                # Whatever how long to sleep, just meaning it's running.
                time.sleep(5)

            (flexmock(plugin_based.inner.DockerBuildWorkflow)
             .should_receive("build_docker_image")
             .replace_with(_build_docker_image))

            proc = multiprocessing.Process(target=task.execute)
            proc.start()

            # wait a short a while for the task.execute to run in the separate process.
            time.sleep(0.3)
            proc.terminate()

        assert context_dir.join("workflow.json").exists()

        wf_data = ImageBuildWorkflowData()
        wf_data.load_from_dir(ContextDir(Path(context_dir)))
        # As long as the data is loaded successfully, just check some
        # attributes to check the data.
        assert DockerfileImages() == wf_data.dockerfile_images
        assert {} == wf_data.prebuild_results

    def test_workflow_data_is_restored_before_starting_to_build(self, build_dir, tmpdir):
        context_dir = tmpdir.join("context_dir").mkdir()

        # Write workflow data as it was saved by a previous task
        data = ImageBuildWorkflowData()
        # Note: for this test, dockerfile_images can't be passed as a kwarg to
        # the ImageBuildWorkflowData directly due to the flexmock of ImageBuildWorkflowData
        # in the fixture, otherwise
        # "TypeError: object.__new__() takes exactly one argument (the type to instantiate)"
        # will be raised. So far, have no idea why it happens.
        data.dockerfile_images = DockerfileImages(["scratch"])
        data.tag_conf.add_floating_image("registry/app:latest")
        data.prebuild_results["plugin_a"] = {"var": "value"}
        data.save(ContextDir(Path(context_dir)))

        params = TaskParams(build_dir=str(build_dir),
                            context_dir=str(context_dir),
                            config_file="config.yaml",
                            user_params={})
        (flexmock(params)
         .should_receive("source")
         .and_return(DummySource("git", "https://git.host/")))

        task = plugin_based.PluginBasedTask(params)

        class _FakeDockerBuildWorkflow:
            def __init__(self, build_dir, data=None, **kwargs):
                self.data = data

            def build_docker_image(self) -> BuildResult:
                assert DockerfileImages(["scratch"]) == self.data.dockerfile_images
                return BuildResult(logs=["Build successfully."])

        (flexmock(plugin_based.inner)
         .should_receive("DockerBuildWorkflow")
         .replace_with(_FakeDockerBuildWorkflow))

        task.execute()
