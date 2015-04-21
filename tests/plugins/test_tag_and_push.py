from __future__ import print_function

from dock.core import DockerTasker
from dock.inner import DockerBuildWorkflow
from dock.plugin import PostBuildPluginsRunner
from dock.plugins.post_tag_and_push import TagAndPushPlugin
from dock.util import ImageName
from tests.constants import LOCALHOST_REGISTRY, TEST_IMAGE, INPUT_IMAGE


class X(object):
    image_id = INPUT_IMAGE
    git_dockerfile_path = None
    git_path = None
    base_image = ImageName(repo="qwe", tag="asd")


def test_tag_and_push_plugin(tmpdir):
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow("asd", "test-image")
    setattr(workflow, 'builder', X)

    runner = PostBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': TagAndPushPlugin.key,
            'args': {
                "mapping": {
                    LOCALHOST_REGISTRY: {
                        "insecure": True,
                        "image_names": [
                            TEST_IMAGE
                        ]
                    }
                }
            }
        }]
    )
    output = runner.run()
    image = output[TagAndPushPlugin.key][0]
    tasker.remove_image(image)
