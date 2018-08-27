"""
Copyright (c) 2018 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import pytest
from flexmock import flexmock

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.plugins.pre_change_from_in_df import (
    NoIdInspection, BaseImageMismatch, ParentImageUnresolved, ChangeFromPlugin, ParentImageMissing
)
from atomic_reactor.util import ImageName, df_parser
from tests.fixtures import docker_tasker
from tests.constants import SOURCE
from tests.stubs import StubInsideBuilder
from textwrap import dedent


def mock_workflow():
    """
    Provide just enough structure that workflow can be used to run the plugin.
    Defaults below are solely to enable that purpose; tests where those values
    matter should provide their own.
    """

    workflow = DockerBuildWorkflow(SOURCE, "mock:default_built")
    builder = StubInsideBuilder().for_workflow(workflow)
    builder.set_df_path('/mock-path')
    builder.parent_images["mock:base"] = "mock:tag"
    builder.base_image = ImageName.parse("mock:tag")
    builder.tasker = flexmock()
    workflow.builder = flexmock(builder)

    return workflow


def run_plugin(workflow, allow_failure=False):
    result = PreBuildPluginsRunner(
       docker_tasker(), workflow,
       [{
          'name': ChangeFromPlugin.key,
          'args': {},
       }]
    ).run()

    if not allow_failure:  # exceptions are captured in plugin result
        assert result[ChangeFromPlugin.key] is None, "Plugin threw exception, check logs"

    return result[ChangeFromPlugin.key]


def test_update_base_image(tmpdir):
    df_content = dedent("""\
        FROM {}
        LABEL horses=coconuts
        CMD whoah
    """)
    dfp = df_parser(str(tmpdir))
    dfp.content = df_content.format("base:image")
    image_name = "base@sha256:1234"

    workflow = mock_workflow()
    workflow.builder.set_df_path(dfp.dockerfile_path)
    workflow.builder.parent_images = {"base:image": image_name}
    workflow.builder.base_image = ImageName.parse(image_name)
    workflow.builder.set_parent_inspection_data(image_name, dict(Id="base@sha256:1234"))
    workflow.builder.tasker.inspect_image = lambda *_: dict(Id="base@sha256:1234")

    run_plugin(workflow)
    expected_df = df_content.format("base@sha256:1234")
    assert dfp.content == expected_df


def test_update_base_image_inspect_broken(tmpdir, caplog):
    """exercise code branch where the base image inspect comes back without an Id"""
    df_content = "FROM base:image"
    dfp = df_parser(str(tmpdir))
    dfp.content = df_content
    image_name = "base@sha256:1234"

    workflow = mock_workflow()
    workflow.builder.set_df_path(dfp.dockerfile_path)
    workflow.builder.parent_images = {"base:image": image_name}
    workflow.builder.base_image = ImageName.parse(image_name)
    workflow.builder.set_parent_inspection_data(image_name, dict(no_id="here"))

    with pytest.raises(NoIdInspection):
        ChangeFromPlugin(docker_tasker(), workflow).run()
    assert dfp.content == df_content  # nothing changed
    assert "missing in inspection" in caplog.text()


def test_update_parent_images(tmpdir):
    """test the happy path for updating multiple parents"""
    df_content = dedent("""\
        FROM first:parent AS builder1
        CMD build /spam/eggs
        FROM second:parent AS builder2
        CMD build /vikings
        FROM monty
        COPY --from=builder1 /spam/eggs /bin/eggs
        COPY --from=builder2 /vikings /bin/vikings
    """)
    dfp = df_parser(str(tmpdir))
    dfp.content = df_content

    # maps from dockerfile image to unique tag and then to ID
    pimgs = {
        "first:parent": 'build-name:1',
        "second:parent": 'build-name:2',
        "monty": 'build-name:3',
    }
    img_ids = {
        'build-name:1': 'id:1',
        'build-name:2': 'id:2',
        'build-name:3': 'id:3',
    }

    workflow = mock_workflow()
    workflow.builder.set_df_path(dfp.dockerfile_path)
    workflow.builder.base_image = ImageName.parse(pimgs['monty'])
    workflow.builder.parent_images = pimgs
    workflow.builder.tasker.inspect_image = lambda img: dict(Id=img_ids[img])
    for image_name, image_id in img_ids.items():
        workflow.builder.set_parent_inspection_data(image_name, dict(Id=image_id))

    run_plugin(workflow)
    expected_df_content = df_content
    for image, rename in pimgs.items():
        expected_df_content = expected_df_content.replace(image, img_ids[rename])
    assert dfp.content == expected_df_content


def test_parent_images_unresolved(tmpdir):
    """test when parent_images hasn't been filled in with unique tags."""
    dfp = df_parser(str(tmpdir))
    dfp.content = "FROM spam"

    workflow = mock_workflow()
    workflow.builder.set_df_path(dfp.dockerfile_path)
    workflow.builder.base_image = ImageName.parse('eggs')

    # we want to fail because some img besides base was not resolved
    workflow.builder.parent_images = {'spam': 'eggs:latest', 'extra:image': None}

    with pytest.raises(ParentImageUnresolved):
        ChangeFromPlugin(docker_tasker(), workflow).run()


def test_parent_images_missing(tmpdir):
    """test when parent_images has been mangled and lacks parents compared to dockerfile."""
    dfp = df_parser(str(tmpdir))
    dfp.content = dedent("""\
        FROM first:parent AS builder1
        FROM second:parent AS builder2
        FROM monty
    """)

    workflow = mock_workflow()
    workflow.builder.set_df_path(dfp.dockerfile_path)
    workflow.builder.parent_images = {"monty": "build-name:3"}
    workflow.builder.base_image = ImageName.parse("build-name:3")

    with pytest.raises(ParentImageMissing):
        ChangeFromPlugin(docker_tasker(), workflow).run()


def test_parent_images_mismatch_base_image(tmpdir):
    """test when base_image has been updated differently from parent_images."""
    dfp = df_parser(str(tmpdir))
    dfp.content = "FROM base:image"
    workflow = mock_workflow()
    workflow.builder.set_df_path(dfp.dockerfile_path)
    workflow.builder.base_image = ImageName.parse("base:image")
    workflow.builder.parent_images = {"base:image": "different-parent-tag"}

    with pytest.raises(BaseImageMismatch):
        ChangeFromPlugin(docker_tasker(), workflow).run()
