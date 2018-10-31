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
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor.util import ImageName, df_parser
from tests.constants import SOURCE
from tests.stubs import StubInsideBuilder, StubSource
from textwrap import dedent


def mock_workflow():
    """
    Provide just enough structure that workflow can be used to run the plugin.
    Defaults below are solely to enable that purpose; tests where those values
    matter should provide their own.
    """

    workflow = DockerBuildWorkflow(SOURCE, "mock:default_built")
    workflow.source = StubSource()
    builder = StubInsideBuilder().for_workflow(workflow)
    builder.set_df_path('/mock-path')
    base_image_name = ImageName.parse("mock:tag")
    builder.parent_images[ImageName.parse("mock:base")] = base_image_name
    builder.base_image = base_image_name
    builder.tasker = flexmock()
    workflow.builder = flexmock(builder)

    return workflow


def run_plugin(workflow, reactor_config_map, docker_tasker, allow_failure=False, organization=None):
    if reactor_config_map:
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig({'version': 1,
                           'registries_organization': organization})

    result = PreBuildPluginsRunner(
       docker_tasker, workflow,
       [{
          'name': ChangeFromPlugin.key,
          'args': {},
       }]
    ).run()

    if not allow_failure:  # exceptions are captured in plugin result
        assert result[ChangeFromPlugin.key] is None, "Plugin threw exception, check logs"

    return result[ChangeFromPlugin.key]


@pytest.mark.parametrize('organization', [None, 'my_organization'])
def test_update_base_image(organization, tmpdir, reactor_config_map, docker_tasker):
    df_content = dedent("""\
        FROM {}
        LABEL horses=coconuts
        CMD whoah
    """)
    dfp = df_parser(str(tmpdir))
    image_str = "base:image"
    dfp.content = df_content.format(image_str)
    base_str = "base@sha256:1234"
    base_image_name = ImageName.parse("base@sha256:1234")

    enclosed_parent = ImageName.parse(image_str)
    if organization and reactor_config_map:
        enclosed_parent.enclose(organization)

    workflow = mock_workflow()
    workflow.builder.set_df_path(dfp.dockerfile_path)
    workflow.builder.parent_images = {enclosed_parent: base_image_name}
    workflow.builder.base_image = base_image_name
    workflow.builder.set_parent_inspection_data(base_str, dict(Id=base_str))
    workflow.builder.tasker.inspect_image = lambda *_: dict(Id=base_str)

    run_plugin(workflow, reactor_config_map, docker_tasker, organization=organization)
    expected_df = df_content.format(base_str)
    assert dfp.content == expected_df


def test_update_base_image_inspect_broken(tmpdir, caplog, docker_tasker):
    """exercise code branch where the base image inspect comes back without an Id"""
    df_content = "FROM base:image"
    dfp = df_parser(str(tmpdir))
    dfp.content = df_content
    image_str = "base@sha256:1234"
    image_name = ImageName.parse(image_str)

    workflow = mock_workflow()
    workflow.builder.set_df_path(dfp.dockerfile_path)
    workflow.builder.parent_images = {ImageName.parse("base:image"): image_name}
    workflow.builder.base_image = image_name
    workflow.builder.set_parent_inspection_data(image_str, dict(no_id="here"))

    with pytest.raises(NoIdInspection):
        ChangeFromPlugin(docker_tasker, workflow).run()
    assert dfp.content == df_content  # nothing changed
    assert "missing in inspection" in caplog.text()


@pytest.mark.parametrize('organization', [None, 'my_organization'])  # noqa
@pytest.mark.parametrize(('df_content, expected_df_content, base_from_scratch'), [
    (
        dedent("""\
            FROM first:parent AS builder1
            CMD build /spam/eggs
            FROM second:parent AS builder2
            CMD build /vikings
            FROM monty
            COPY --from=builder1 /spam/eggs /bin/eggs
            COPY --from=builder2 /vikings /bin/vikings
        """),
        dedent("""\
            FROM id:1 AS builder1
            CMD build /spam/eggs
            FROM id:2 AS builder2
            CMD build /vikings
            FROM id:3
            COPY --from=builder1 /spam/eggs /bin/eggs
            COPY --from=builder2 /vikings /bin/vikings
        """),
        False,
    ),
    (
        dedent("""\
            FROM first:parent AS builder1
            CMD build /spam/eggs
            FROM second:parent AS builder2
            CMD build /vikings
            FROM scratch
            CMD build /from_scratch
            FROM monty
            COPY --from=builder1 /spam/eggs /bin/eggs
            COPY --from=builder2 /vikings /bin/vikings
        """),
        dedent("""\
            FROM id:1 AS builder1
            CMD build /spam/eggs
            FROM id:2 AS builder2
            CMD build /vikings
            FROM scratch
            CMD build /from_scratch
            FROM id:3
            COPY --from=builder1 /spam/eggs /bin/eggs
            COPY --from=builder2 /vikings /bin/vikings
        """),
        False,
    ),
    (
        dedent("""\
            FROM first:parent AS builder1
            CMD build /spam/eggs
            FROM second:parent AS builder2
            CMD build /vikings
            FROM scratch
            CMD build /from_scratch
            FROM monty
            COPY --from=builder1 /spam/eggs /bin/eggs
            COPY --from=builder2 /vikings /bin/vikings
            FROM scratch
            CMD build /from_scratch2
        """),
        dedent("""\
            FROM id:1 AS builder1
            CMD build /spam/eggs
            FROM id:2 AS builder2
            CMD build /vikings
            FROM scratch
            CMD build /from_scratch
            FROM id:3
            COPY --from=builder1 /spam/eggs /bin/eggs
            COPY --from=builder2 /vikings /bin/vikings
            FROM scratch
            CMD build /from_scratch2
        """),
        True,
    ),
])
def test_update_parent_images(organization, df_content, expected_df_content, base_from_scratch,
                              tmpdir, reactor_config_map, docker_tasker):
    """test the happy path for updating multiple parents"""
    dfp = df_parser(str(tmpdir))
    dfp.content = df_content

    # maps from dockerfile image to unique tag and then to ID
    first = ImageName.parse("first:parent")
    second = ImageName.parse("second:parent")
    monty = ImageName.parse("monty")
    build1 = ImageName.parse('build-name:1')
    build2 = ImageName.parse('build-name:2')
    build3 = ImageName.parse('build-name:3')
    if organization and reactor_config_map:
        first.enclose(organization)
        second.enclose(organization)
        monty.enclose(organization)
    pimgs = {
        first: build1,
        second: build2,
        monty: build3,
    }
    img_ids = {
        'build-name:1': 'id:1',
        'build-name:2': 'id:2',
        'build-name:3': 'id:3',
    }

    workflow = mock_workflow()
    workflow.builder.set_df_path(dfp.dockerfile_path)
    workflow.builder.set_base_from_scratch(base_from_scratch)
    workflow.builder.base_image = ImageName.parse('build-name:3')
    workflow.builder.parent_images = pimgs
    workflow.builder.tasker.inspect_image = lambda img: dict(Id=img_ids[img])
    for image_name, image_id in img_ids.items():
        workflow.builder.set_parent_inspection_data(image_name, dict(Id=image_id))

    original_base = workflow.builder.base_image
    run_plugin(workflow, reactor_config_map, docker_tasker, organization=organization)
    assert dfp.content == expected_df_content
    if base_from_scratch:
        assert original_base == workflow.builder.base_image


def test_parent_images_unresolved(tmpdir, docker_tasker):
    """test when parent_images hasn't been filled in with unique tags."""
    dfp = df_parser(str(tmpdir))
    dfp.content = "FROM spam"

    workflow = mock_workflow()
    workflow.builder.set_df_path(dfp.dockerfile_path)
    workflow.builder.base_image = ImageName.parse('eggs')
    # we want to fail because some img besides base was not resolved
    workflow.builder.parent_images = {
       ImageName.parse('spam'): ImageName.parse('eggs'),
       ImageName.parse('extra:image'): None
    }

    with pytest.raises(ParentImageUnresolved):
        ChangeFromPlugin(docker_tasker, workflow).run()


def test_parent_images_missing(tmpdir, docker_tasker):
    """test when parent_images has been mangled and lacks parents compared to dockerfile."""
    dfp = df_parser(str(tmpdir))
    dfp.content = dedent("""\
        FROM first:parent AS builder1
        FROM second:parent AS builder2
        FROM monty
    """)

    workflow = mock_workflow()
    workflow.builder.set_df_path(dfp.dockerfile_path)
    workflow.builder.parent_images = {ImageName.parse("monty"): ImageName.parse("build-name:3")}
    workflow.builder.base_image = ImageName.parse("build-name:3")

    with pytest.raises(ParentImageMissing):
        ChangeFromPlugin(docker_tasker, workflow).run()


def test_parent_images_mismatch_base_image(tmpdir, docker_tasker):
    """test when base_image has been updated differently from parent_images."""
    dfp = df_parser(str(tmpdir))
    dfp.content = "FROM base:image"
    workflow = mock_workflow()
    workflow.builder.set_df_path(dfp.dockerfile_path)
    workflow.builder.base_image = ImageName.parse("base:image")
    workflow.builder.parent_images = {
       ImageName.parse("base:image"): ImageName.parse("different-parent-tag")
    }

    with pytest.raises(BaseImageMismatch):
        ChangeFromPlugin(docker_tasker, workflow).run()
