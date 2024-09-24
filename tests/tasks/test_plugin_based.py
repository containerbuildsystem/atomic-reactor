"""
Copyright (c) 2021-2022 Red Hat, Inc
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

from atomic_reactor.inner import ImageBuildWorkflowData
from atomic_reactor.plugin import TaskCanceledException, PluginFailedException
from atomic_reactor.tasks.common import TaskParams
from atomic_reactor.tasks.binary import InitTaskParams, BinaryPreBuildTask, BinaryInitTask
from atomic_reactor.util import DockerfileImages

from atomic_reactor import inner, dirs
from atomic_reactor.dirs import RootBuildDir, ContextDir
from atomic_reactor.tasks import plugin_based


class TestPluginBasedTask:
    """Tests for the PluginBasedTask class"""

    @pytest.fixture
    def task_with_mocked_deps(self, monkeypatch, context_dir, build_dir, dummy_source, tmpdir):
        """Create a PluginBasedTask instance with mocked task parameters.

        Mock DockerBuildWorkflow accordingly. Return the mocked workflow instance for further
        customization in individual tests.
        """
        task_params = TaskParams(build_dir=build_dir,
                                 config_file="config.yaml",
                                 context_dir=str(context_dir),
                                 namespace="test-namespace",
                                 pipeline_run_name='test-pipeline-run',
                                 user_params={"a": "b"},
                                 task_result='results')

        expect_source = dummy_source
        (flexmock(task_params)
         .should_receive("source")
         .and_return(expect_source))

        expect_plugins = []
        monkeypatch.setattr(plugin_based.PluginBasedTask, "plugins_conf", expect_plugins)

        root_build_dir = RootBuildDir(build_dir)

        # Help to verify the RootBuildDir object is passed to the workflow object.
        (flexmock(plugin_based.PluginBasedTask)
         .should_receive("get_build_dir")
         .and_return(root_build_dir))

        # The test methods inside this test case do not involve the workflow
        # data. Thanks the dataclass, flexmock is able to assert the workflow
        # data object, which is created during task execution, is the same as
        # this one, that is they are all workflow data objects and the data
        # included are same.
        wf_data = ImageBuildWorkflowData()

        mocked_workflow = flexmock(inner.DockerBuildWorkflow)
        (
            mocked_workflow
            .should_call("__init__")
            .once()
            .with_args(
                context_dir=ContextDir,
                build_dir=root_build_dir,
                data=wf_data,
                namespace="test-namespace",
                pipeline_run_name='test-pipeline-run',
                source=expect_source,
                plugins_conf=expect_plugins,
                user_params={"a": "b"},
                reactor_config_path="config.yaml",
                keep_plugins_running=False,
            )
        )
        mocked_workflow.should_receive("build_container_image").and_raise(
            AssertionError("you must mock the build_container_image() workflow method")
        )

        task = plugin_based.PluginBasedTask(task_params)
        return task, mocked_workflow

    @pytest.mark.parametrize("call_init_build_dirs", [True, False])
    def test_execute(self, task_with_mocked_deps, caplog, call_init_build_dirs):
        task, mocked_workflow = task_with_mocked_deps

        mocked_workflow.should_receive("build_container_image").once()
        flexmock(dirs.RootBuildDir).should_call('init_build_dirs').times(int(call_init_build_dirs))

        task.execute(init_build_dirs=call_init_build_dirs)
        assert r"task default finished successfully \o/" in caplog.text

    def test_execute_raises_exception(self, task_with_mocked_deps, caplog):
        task, mocked_workflow = task_with_mocked_deps

        error = ValueError("something went wrong")
        mocked_workflow.should_receive("build_container_image").and_raise(error)

        with pytest.raises(ValueError, match="something went wrong"):
            task.execute()

        assert "task default failed: something went wrong" in caplog.text

    def test_execute_returns_failure(self, task_with_mocked_deps, caplog):
        task, mocked_workflow = task_with_mocked_deps

        err = PluginFailedException("something is wrong")
        mocked_workflow.should_receive("build_container_image").and_raise(err)

        with pytest.raises(PluginFailedException, match="something is wrong"):
            task.execute()

        assert "task default failed: something is wrong" in caplog.text


@pytest.mark.parametrize(
    "build_result",
    ["normal_return", "error_raised", "terminated"]
)
def test_ensure_workflow_data_is_saved_in_various_conditions(
    build_result, build_dir, dummy_source, tmpdir
):
    context_dir = tmpdir.join("context_dir").mkdir()
    params = TaskParams(build_dir=str(build_dir),
                        config_file="config.yaml",
                        context_dir=str(context_dir),
                        namespace="test-namespace",
                        pipeline_run_name='test-pipeline-run',
                        user_params={},
                        task_result='results')
    (flexmock(params)
     .should_receive("source")
     .and_return(dummy_source))

    task = plugin_based.PluginBasedTask(params)

    if build_result == "normal_return":
        (flexmock(plugin_based.inner.DockerBuildWorkflow)
         .should_receive("build_container_image")
         .once())

        task.run()

    elif build_result == "error_raised":
        (flexmock(plugin_based.inner.DockerBuildWorkflow)
         .should_receive("build_container_image")
         .and_raise(TaskCanceledException))

        with pytest.raises(TaskCanceledException):
            task.run()

    elif build_result == "terminated":
        # Start the task.run in a separate process and terminate it.
        # This simulates the Cancel behavior by TERM signal.

        def _build_container_image(*args, **kwargs):

            def _cancel_build(*args, **kwargs):
                raise TaskCanceledException()

            signal.signal(signal.SIGTERM, _cancel_build)
            # Whatever how long to sleep, just meaning it's running.
            time.sleep(5)

        (flexmock(plugin_based.inner.DockerBuildWorkflow)
         .should_receive("build_container_image")
         .replace_with(_build_container_image))

        proc = multiprocessing.Process(target=task.run)
        proc.start()

        # wait a short a while for the task.run to run in the separate process.
        time.sleep(0.3)
        proc.terminate()

    time.sleep(1)
    assert context_dir.join("workflow.json").exists()

    wf_data = ImageBuildWorkflowData()
    wf_data.load_from_dir(ContextDir(Path(context_dir)))
    # As long as the data is loaded successfully, just check some
    # attributes to check the data.
    assert DockerfileImages() == wf_data.dockerfile_images
    assert {} == wf_data.plugins_results


def test_ensure_workflow_data_is_saved_init_task(
    build_dir, dummy_source, tmpdir
):
    context_dir = tmpdir.join("context_dir").mkdir()
    params = InitTaskParams(build_dir=str(build_dir),
                            config_file="config.yaml",
                            context_dir=str(context_dir),
                            namespace="test-namespace",
                            pipeline_run_name='test-pipeline-run',
                            user_params={},
                            task_result='results',
                            platforms_result='platforms_result')
    (flexmock(params)
     .should_receive("source")
     .and_return(dummy_source))

    task = BinaryInitTask(params)

    (flexmock(plugin_based.inner.DockerBuildWorkflow)
     .should_receive("build_container_image")
     .once())

    task.run()
    time.sleep(1)
    assert context_dir.join("workflow.json").exists()

    wf_data = ImageBuildWorkflowData()
    wf_data.load_from_dir(ContextDir(Path(context_dir)))
    # As long as the data is loaded successfully, just check some
    # attributes to check the data.
    assert DockerfileImages() == wf_data.dockerfile_images
    assert {} == wf_data.plugins_results


def test_ensure_workflow_data_is_saved_prebuild_task(
    build_dir, dummy_source, tmpdir
):
    context_dir = tmpdir.join("context_dir").mkdir()
    params = TaskParams(build_dir=str(build_dir),
                        config_file="config.yaml",
                        context_dir=str(context_dir),
                        namespace="test-namespace",
                        pipeline_run_name='test-pipeline-run',
                        user_params={},
                        task_result='results')
    (flexmock(params)
     .should_receive("source")
     .and_return(dummy_source))

    task = BinaryPreBuildTask(params)

    (flexmock(plugin_based.inner.DockerBuildWorkflow)
     .should_receive("build_container_image")
     .once())

    task.run()
    time.sleep(1)
    assert context_dir.join("workflow.json").exists()

    wf_data = ImageBuildWorkflowData()
    wf_data.load_from_dir(ContextDir(Path(context_dir)))
    # As long as the data is loaded successfully, just check some
    # attributes to check the data.
    assert DockerfileImages() == wf_data.dockerfile_images
    assert {} == wf_data.plugins_results


def test_workflow_data_is_restored_before_starting_to_build(build_dir, dummy_source, tmpdir):
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
    data.plugins_results["plugin_a"] = {"var": "value"}
    data.save(ContextDir(Path(context_dir)))

    params = TaskParams(build_dir=str(build_dir),
                        config_file="config.yaml",
                        context_dir=str(context_dir),
                        namespace="test-namespace",
                        pipeline_run_name='test-pipeline-run',
                        user_params={},
                        task_result='results')
    (flexmock(params)
     .should_receive("source")
     .and_return(dummy_source))

    task = plugin_based.PluginBasedTask(params)

    class _FakeDockerBuildWorkflow:
        def __init__(self, build_dir, data=None, **kwargs):
            self.data = data

        def build_container_image(self):
            assert DockerfileImages(["scratch"]) == self.data.dockerfile_images

    (flexmock(plugin_based.inner)
     .should_receive("DockerBuildWorkflow")
     .replace_with(_FakeDockerBuildWorkflow))

    task.execute()
