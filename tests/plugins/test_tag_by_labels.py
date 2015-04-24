from __future__ import print_function

from dock.core import DockerTasker
from dock.inner import DockerBuildWorkflow
from dock.plugin import PostBuildPluginsRunner
from dock.plugins.post_tag_and_push import TagAndPushPlugin
from dock.plugins.post_tag_by_labels import TagByLabelsPlugin
from dock.util import ImageName
from tests.constants import LOCALHOST_REGISTRY, TEST_IMAGE, INPUT_IMAGE, MOCK

if MOCK:
    from tests.docker_mock import mock_docker


class X(object):
    image_id = INPUT_IMAGE
    git_dockerfile_path = None
    git_path = None
    base_image = ImageName(repo="qwe", tag="asd")


def test_tag_by_labels_plugin(tmpdir):
    if MOCK:
        mock_docker()

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
    image = ImageName(repo=TEST_IMAGE,
                      tag="%s_%s" % (version, release),
                      registry=LOCALHOST_REGISTRY)

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
    tasker.remove_image(image)
