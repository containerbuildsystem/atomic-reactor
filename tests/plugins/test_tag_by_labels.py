from __future__ import print_function

from dock.core import DockerTasker
from dock.inner import DockerBuildWorkflow
from dock.plugin import PostBuildPluginsRunner
from dock.plugins.post_tag_and_push import TagAndPushPlugin
from dock.plugins.post_tag_by_labels import TagByLabelsPlugin
from tests.constants import LOCALHOST_REGISTRY, TEST_IMAGE, INPUT_IMAGE


class X(object):
    image_id = INPUT_IMAGE
    git_dockerfile_path = None
    git_path = None
    base_image_name = "qwe"
    base_tag = "asd"


def test_tag_by_labels_plugin(tmpdir):
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow("asd", "test-image")
    version = "1.0"
    release = "1"
    workflow.built_image_inspect = {
        "ContainerConfig": {
            "Labels": {
                "Name": TEST_IMAGE,
                "Version": version,
                "Release": release
            }
        }
    }
    image = "%s:%s_%s" % (TEST_IMAGE, version, release)

    setattr(workflow, 'builder', X)

    runner = PostBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': TagByLabelsPlugin.key,
            'args': {
                "registry_uri": LOCALHOST_REGISTRY,
                "insecure": True,
            }
        }, {
            'name': TagAndPushPlugin.key,
        }]
    )
    output = runner.run()
    assert output[TagAndPushPlugin.key]
    tasker.remove_image(LOCALHOST_REGISTRY + "/" + image)
