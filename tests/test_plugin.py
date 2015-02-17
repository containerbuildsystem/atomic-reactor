from dock.core import DockerTasker
from dock.inner import DockerBuildWorkflow
from dock.plugin import PreBuildPluginsRunner, PostBuildPluginsRunner
from dock.plugins.post_rpmqa import PostBuildRPMqaPlugin


git_url = "https://github.com/TomasTomecek/docker-hello-world.git"
TEST_IMAGE = "fedora:latest"


def test_load_prebuild_plugins():
    runner = PreBuildPluginsRunner(DockerTasker(), DockerBuildWorkflow("", ""), None)
    assert runner.plugin_classes is not None
    assert len(runner.plugin_classes) > 0


def test_load_postbuild_plugins():
    runner = PostBuildPluginsRunner(DockerTasker(), DockerBuildWorkflow("", ""), None)
    assert runner.plugin_classes is not None
    assert len(runner.plugin_classes) > 0


class X(object):
    pass


def test_rpmqa_plugin():
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(git_url, "test-image")
    setattr(workflow, 'builder', X)
    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'base_image_name', "fedora")
    setattr(workflow.builder, 'base_tag', "21")
    runner = PostBuildPluginsRunner(tasker, workflow,
                                    [{"name": PostBuildRPMqaPlugin.key,
                                      "args": {'image_id': TEST_IMAGE}}])
    results = runner.run()
    assert results is not None
    assert results[PostBuildRPMqaPlugin.key] is not None
    assert len(results[PostBuildRPMqaPlugin.key]) > 0
