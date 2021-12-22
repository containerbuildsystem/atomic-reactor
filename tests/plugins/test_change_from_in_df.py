"""
Copyright (c) 2018-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from textwrap import dedent

import pytest
from flexmock import flexmock
from osbs.utils import ImageName

from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_change_from_in_df import ChangeFromPlugin
from atomic_reactor.util import df_parser, DockerfileImages
from tests.stubs import StubSource

pytestmark = pytest.mark.usefixtures('user_params')


def mock_workflow(workflow, df_path=None, df_images=None):
    """
    Provide just enough structure that workflow can be used to run the plugin.
    Defaults below are solely to enable that purpose; tests where those values
    matter should provide their own.
    """
    if df_images is None:
        df_images = []
    workflow.source = StubSource()
    flexmock(workflow, df_path=df_path)
    if df_images:
        workflow.dockerfile_images = DockerfileImages(df_images)
    else:
        workflow.dockerfile_images = DockerfileImages(['mock:base'])
        workflow.dockerfile_images['mock:base'] = ImageName.parse("mock:tag")

    return workflow


def run_plugin(workflow):
    result = PreBuildPluginsRunner(
        workflow,
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
def test_update_base_image(tmpdir, workflow, base_image):
    df_content = dedent("""\
        FROM {}
        LABEL horses=coconuts
        CMD whoah
    """)
    dfp = df_parser(str(tmpdir))
    dfp.content = df_content.format(base_image)
    base_str = "base@sha256:1234"
    local_tag = ImageName.parse("base@sha256:1234")

    workflow = mock_workflow(workflow,
                             df_path=dfp.dockerfile_path,
                             df_images=dfp.parent_images)
    workflow.dockerfile_images[base_image] = local_tag

    run_plugin(workflow)
    expected_df = df_content.format(base_str)
    assert dfp.content == expected_df


@pytest.mark.parametrize('df_content, expected_df_content', [
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
            FROM first@sha256:12345 AS builder1
            CMD build /spam/eggs
            FROM second@sha256:12345 AS builder2
            CMD build /vikings
            FROM monty@sha256:12345
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
            FROM first@sha256:12345 AS builder1
            CMD build /spam/eggs
            FROM second@sha256:12345 AS builder2
            CMD build /vikings
            FROM monty@sha256:12345
            COPY --from=builder1 /spam/eggs /bin/eggs
            COPY --from=builder2 /vikings /bin/vikings
            FROM monty@sha256:12345
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
            FROM first@sha256:12345 AS builder1
            CMD build /spam/eggs
            FROM second@sha256:12345 AS builder2
            CMD build /vikings
            FROM monty@sha256:12345
            CMD build /custom
            FROM monty@sha256:12345
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
            FROM first@sha256:12345 AS builder1
            CMD build /spam/eggs
            FROM second@sha256:12345 AS builder2
            CMD build /vikings
            FROM scratch
            CMD build /from_scratch
            FROM monty@sha256:12345
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
            FROM first@sha256:12345 AS builder1
            CMD build /spam/eggs
            FROM second@sha256:12345 AS builder2
            CMD build /vikings
            FROM scratch
            CMD build /from_scratch
            FROM monty@sha256:12345
            COPY --from=builder1 /spam/eggs /bin/eggs
            COPY --from=builder2 /vikings /bin/vikings
            FROM scratch
            CMD build /from_scratch2
        """),
    ),
    (
            dedent("""\
            FROM first:parent AS builder1
            CMD build /spam/eggs
            FROM second:parent AS builder2
            CMD build /vikings
            FROM some.registry.io/third:parent AS builder3
            CMD build /romans
            FROM scratch
            CMD build /from_scratch
            FROM monty
            COPY --from=builder1 /spam/eggs /bin/eggs
            COPY --from=builder2 /vikings /bin/vikings
            COPY --from=builder3 /romans /bin/romans
            FROM scratch
            CMD build /from_scratch2
        """),
            dedent("""\
            FROM first@sha256:12345 AS builder1
            CMD build /spam/eggs
            FROM second@sha256:12345 AS builder2
            CMD build /vikings
            FROM some.registry.io/third@sha256:12345 AS builder3
            CMD build /romans
            FROM scratch
            CMD build /from_scratch
            FROM monty@sha256:12345
            COPY --from=builder1 /spam/eggs /bin/eggs
            COPY --from=builder2 /vikings /bin/vikings
            COPY --from=builder3 /romans /bin/romans
            FROM scratch
            CMD build /from_scratch2
        """),
    ),
])
def test_update_parent_images(df_content, expected_df_content, tmpdir, workflow):
    """test the happy path for updating multiple parents"""
    dfp = df_parser(str(tmpdir))
    dfp.content = df_content

    # maps from dockerfile image to image with manifest digest
    first = ImageName.parse("first:parent")
    second = ImageName.parse("second:parent")
    third = ImageName.parse("some.registry.io/third:parent")
    monty = ImageName.parse("monty")
    custom = ImageName.parse("koji/image-build")
    build1 = ImageName.parse('first@sha256:12345')
    build2 = ImageName.parse('second@sha256:12345')
    build3 = ImageName.parse('monty@sha256:12345')
    build4 = ImageName.parse('some.registry.io/third@sha256:12345')
    parent_images = {
        first: build1,
        second: build2,
        third: build4,
        monty: build3,
        custom: build3,
    }

    workflow = mock_workflow(workflow,
                             df_path=dfp.dockerfile_path,
                             df_images=dfp.parent_images)

    for parent in dfp.parent_images:
        if parent == 'scratch':
            continue
        parent_in = ImageName.parse(parent)
        workflow.dockerfile_images[parent] = parent_images[parent_in]

    original_base = workflow.dockerfile_images.base_image
    run_plugin(workflow)
    assert dfp.content == expected_df_content
    if workflow.dockerfile_images.base_from_scratch:
        assert original_base == workflow.dockerfile_images.base_image


def test_parent_images_unresolved(tmpdir, workflow):
    """test when parent_images hasn't been filled in with unique tags."""
    dfp = df_parser(str(tmpdir))
    dfp.content = dedent("""\
        FROM extra_image
        FROM base_image
    """)

    workflow = mock_workflow(workflow,
                             df_path=dfp.dockerfile_path,
                             df_images=['extra_image', 'base_image'])
    workflow.dockerfile_images['base_image'] = 'base_local'

    with pytest.raises(PluginFailedException) as exc:
        run_plugin(workflow)
    assert "raised an exception: ParentImageUnresolved" in str(exc.value)


def test_parent_images_missing(tmpdir, workflow):
    """test when parent_images has been mangled and lacks parents compared to dockerfile."""
    dfp = df_parser(str(tmpdir))
    dfp.content = dedent("""\
        FROM first:parent AS builder1
        FROM second:parent AS builder2
        FROM monty
    """)

    workflow = mock_workflow(workflow,
                             df_path=dfp.dockerfile_path,
                             df_images=['monty'])
    workflow.dockerfile_images['monty'] = 'monty_local'

    with pytest.raises(PluginFailedException) as exc:
        run_plugin(workflow)
    assert "raised an exception: ParentImageMissing" in str(exc.value)


def test_parent_images_mismatch_base_image(tmpdir, workflow):
    """test when base_image has been updated differently from parent_images."""
    dfp = df_parser(str(tmpdir))
    dfp.content = "FROM base:image"
    workflow = mock_workflow(workflow,
                             df_path=dfp.dockerfile_path,
                             df_images=['base:image', 'parent_different:latest'])

    with pytest.raises(PluginFailedException) as exc:
        run_plugin(workflow)

    assert "raised an exception: BaseImageMismatch" in str(exc.value)
