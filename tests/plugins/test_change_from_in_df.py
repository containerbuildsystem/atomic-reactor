"""
Copyright (c) 2018 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals, absolute_import

import pytest
from flexmock import flexmock

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_change_from_in_df import ChangeFromPlugin
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor.util import df_parser
from osbs.utils import ImageName
from tests.constants import SOURCE
from tests.stubs import StubInsideBuilder, StubSource
from textwrap import dedent

pytestmark = pytest.mark.usefixtures('user_params')


def mock_workflow():
    """
    Provide just enough structure that workflow can be used to run the plugin.
    Defaults below are solely to enable that purpose; tests where those values
    matter should provide their own.
    """

    workflow = DockerBuildWorkflow(source=SOURCE)
    workflow.source = StubSource()
    builder = StubInsideBuilder().for_workflow(workflow)
    builder.set_df_path('/mock-path')
    builder.set_dockerfile_images(['mock:base'])
    builder.dockerfile_images['mock:base'] = ImageName.parse("mock:tag")

    builder.tasker = flexmock()
    workflow.builder = flexmock(builder)

    return workflow


def run_plugin(workflow, docker_tasker):
    workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
    workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
        ReactorConfig({'version': 1})

    result = PreBuildPluginsRunner(
       docker_tasker, workflow,
       [{
          'name': ChangeFromPlugin.key,
          'args': {},
       }]
    ).run()

    return result[ChangeFromPlugin.key]


@pytest.mark.parametrize('base_image', [
    "base:image",
    "different_registry.com/base:image",
])
def test_update_base_image(tmpdir, docker_tasker, base_image):
    df_content = dedent("""\
        FROM {}
        LABEL horses=coconuts
        CMD whoah
    """)
    dfp = df_parser(str(tmpdir))
    dfp.content = df_content.format(base_image)
    base_str = "base@sha256:1234"
    local_tag = ImageName.parse("base@sha256:1234")

    workflow = mock_workflow()
    workflow.builder.set_df_path(dfp.dockerfile_path)
    workflow.builder.set_dockerfile_images(dfp.parent_images)
    workflow.builder.dockerfile_images[base_image] = local_tag
    workflow.builder.set_parent_inspection_data(base_str, dict(Id=base_str))
    workflow.builder.tasker.inspect_image = lambda *_: dict(Id=base_str)

    run_plugin(workflow, docker_tasker)
    expected_df = df_content.format(base_str)
    assert dfp.content == expected_df


def test_update_base_image_inspect_broken(tmpdir, caplog, docker_tasker):
    """exercise code branch where the base image inspect comes back without an Id"""
    df_content = "FROM base:image"
    dfp = df_parser(str(tmpdir))
    dfp.content = df_content
    image_str = "base@sha256:1234"
    local_tag = ImageName.parse(image_str)
    workflow = mock_workflow()
    workflow.builder.set_df_path(dfp.dockerfile_path)
    workflow.builder.set_dockerfile_images(dfp.parent_images)
    workflow.builder.dockerfile_images['base:image'] = local_tag
    workflow.builder.set_parent_inspection_data(image_str, dict(no_id="here"))

    with pytest.raises(PluginFailedException) as exc:
        run_plugin(workflow, docker_tasker)
    assert "raised an exception: NoIdInspection" in str(exc.value)
    assert dfp.content == df_content  # nothing changed
    assert "missing in inspection" in caplog.text


@pytest.mark.parametrize(('df_content, expected_df_content'), [
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
    ),
    (
        dedent("""\
            FROM first:parent AS builder1
            CMD build /spam/eggs
            FROM second:parent AS builder2
            CMD build /vikings
            FROM monty
            COPY --from=builder1 /spam/eggs /bin/eggs
            COPY --from=builder2 /vikings /bin/vikings
            FROM koji/image-build
            CMD build /custom
        """),
        dedent("""\
            FROM id:1 AS builder1
            CMD build /spam/eggs
            FROM id:2 AS builder2
            CMD build /vikings
            FROM id:3
            COPY --from=builder1 /spam/eggs /bin/eggs
            COPY --from=builder2 /vikings /bin/vikings
            FROM id:3
            CMD build /custom
        """),
    ),
    (
        dedent("""\
            FROM first:parent AS builder1
            CMD build /spam/eggs
            FROM second:parent AS builder2
            CMD build /vikings
            FROM koji/image-build
            CMD build /custom
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
            CMD build /custom
            FROM id:3
            COPY --from=builder1 /spam/eggs /bin/eggs
            COPY --from=builder2 /vikings /bin/vikings
        """),
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
    ),
])
def test_update_parent_images(df_content, expected_df_content, tmpdir, docker_tasker):
    """test the happy path for updating multiple parents"""
    dfp = df_parser(str(tmpdir))
    dfp.content = df_content

    # maps from dockerfile image to unique tag and then to ID
    first = ImageName.parse("first:parent")
    second = ImageName.parse("second:parent")
    monty = ImageName.parse("monty")
    custom = ImageName.parse("koji/image-build")
    build1 = ImageName.parse('build-name:1')
    build2 = ImageName.parse('build-name:2')
    build3 = ImageName.parse('build-name:3')
    pimgs = {
        first: build1,
        second: build2,
        monty: build3,
        custom: build3,
    }
    img_ids = {
        'build-name:1': 'id:1',
        'build-name:2': 'id:2',
        'build-name:3': 'id:3',
    }

    workflow = mock_workflow()
    workflow.builder.set_df_path(dfp.dockerfile_path)
    workflow.builder.set_dockerfile_images(dfp.parent_images)

    for parent in dfp.parent_images:
        if parent == 'scratch':
            continue
        parent_in = ImageName.parse(parent)
        workflow.builder.dockerfile_images[parent] = pimgs[parent_in]

    workflow.builder.tasker.inspect_image = lambda img: dict(Id=img_ids[img])
    for image_name, image_id in img_ids.items():
        workflow.builder.set_parent_inspection_data(image_name, dict(Id=image_id))

    original_base = workflow.builder.dockerfile_images.base_image
    run_plugin(workflow, docker_tasker)
    assert dfp.content == expected_df_content
    assert workflow.builder.original_df == df_content
    if workflow.builder.dockerfile_images.base_from_scratch:
        assert original_base == workflow.builder.dockerfile_images.base_image


def test_parent_images_unresolved(tmpdir, docker_tasker):
    """test when parent_images hasn't been filled in with unique tags."""
    dfp = df_parser(str(tmpdir))
    dfp.content = dedent("""\
        FROM extra_image
        FROM base_image
    """)

    workflow = mock_workflow()
    workflow.builder.set_df_path(dfp.dockerfile_path)
    workflow.builder.set_dockerfile_images(['extra_image', 'base_image'])
    workflow.builder.dockerfile_images['base_image'] = 'base_local'

    with pytest.raises(PluginFailedException) as exc:
        run_plugin(workflow, docker_tasker)
    assert "raised an exception: ParentImageUnresolved" in str(exc.value)


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
    workflow.builder.set_dockerfile_images(['monty'])
    workflow.builder.dockerfile_images['monty'] = 'monty_local'

    with pytest.raises(PluginFailedException) as exc:
        run_plugin(workflow, docker_tasker)
    assert "raised an exception: ParentImageMissing" in str(exc.value)


def test_parent_images_mismatch_base_image(tmpdir, docker_tasker):
    """test when base_image has been updated differently from parent_images."""
    dfp = df_parser(str(tmpdir))
    dfp.content = "FROM base:image"
    workflow = mock_workflow()
    workflow.builder.set_df_path(dfp.dockerfile_path)
    workflow.builder.set_dockerfile_images(['base:image', 'parent_different:latest'])

    with pytest.raises(PluginFailedException) as exc:
        run_plugin(workflow, docker_tasker)

    assert "raised an exception: BaseImageMismatch" in str(exc.value)
