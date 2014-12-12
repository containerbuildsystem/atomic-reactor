from dock.core import DockerTasker
from dock.inner import DockerBuildWorkflow
from dock.plugin import PreBuildPluginsRunner, PostBuildPluginsRunner
from dock.plugins.plugin_rpmqa import PostBuildRPMqaPlugin


TEST_IMAGE = "fedora:latest"


def test_load_prebuild_plugins():
    runner = PreBuildPluginsRunner(None, None, None)
    assert runner.plugin_classes is not None
    assert len(runner.plugin_classes) > 0


def test_load_postbuild_plugins():
    runner = PostBuildPluginsRunner(None, None, None)
    assert runner.plugin_classes is not None
    assert len(runner.plugin_classes) > 0


def test_rpmqa_plugin():
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(None, None)
    runner = PostBuildPluginsRunner(tasker, workflow,
                                    {PostBuildRPMqaPlugin.key: {'image_id': TEST_IMAGE}})
    results = runner.run()
    assert results is not None
    assert results[PostBuildRPMqaPlugin.key] is not None
    assert len(results[PostBuildRPMqaPlugin.key]) > 0
