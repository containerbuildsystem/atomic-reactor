"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import os
import pytest
from dock.util import ImageName
from dock.core import DockerTasker
from dock.inner import DockerBuildWorkflow
from dock.plugin import PostBuildPluginsRunner
from dock.plugins.post_cp_built_image import CopyBuiltImagePlugin, DEFAULT_DEST_DIR
from tests.constants import INPUT_IMAGE

class Y(object):
    pass


class X(object):
    image_id = INPUT_IMAGE
    source = Y()
    source.dockerfile_path = None
    source.path = None
    base_image = ImageName(repo="qwe", tag="asd")

@pytest.mark.parametrize('dest_dir', [None, "test_directory"])
def test_cp_built_image(tmpdir, dest_dir):
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow({"provider": "git", "uri": "asd"}, "test-image")
    workflow.builder = X()
    workflow.exported_squashed_image = {"path": os.path.join(str(tmpdir), "image.tar")}
    open(workflow.exported_squashed_image.get("path"), 'a').close()

    runner = PostBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': CopyBuiltImagePlugin.key,
            'args': {
                "secrets": str(tmpdir),
                "dest_dir": dest_dir}
        }]
    )
    runner.run()
    dest_dir = DEFAULT_DEST_DIR if dest_dir is None else dest_dir
    assert os.path.isfile(os.path.join(str(tmpdir), dest_dir, "image.tar"))
