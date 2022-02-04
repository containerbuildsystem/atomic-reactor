"""
Copyright (c) 2018-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from pathlib import Path
from textwrap import dedent

import pytest
from osbs.utils import ImageName

from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.pre_change_from_in_df import ChangeFromPlugin

from tests.mock_env import MockEnv

pytestmark = pytest.mark.usefixtures('user_params')


def mock_workflow(workflow, df_content, override_df_images=None):
    """"" Provide just enough structure that workflow can be used to run the plugin."""
    (Path(workflow.source.path) / "Dockerfile").write_text(df_content)
    workflow.build_dir.init_build_dirs(["aarch64", "x86_64"], workflow.source)

    if override_df_images:
        df_images = override_df_images
    else:
        df_images = workflow.build_dir.any_platform.dockerfile.parent_images

    MockEnv(workflow).set_dockerfile_images(df_images)

    return workflow


def run_plugin(workflow):
    result = MockEnv(workflow).for_plugin("prebuild", ChangeFromPlugin.key).create_runner().run()
    return result[ChangeFromPlugin.key]


def check_df_content(expected_content, workflow):
    def check_df(build_dir):
        assert build_dir.dockerfile_path.read_text() == expected_content

    workflow.build_dir.for_each_platform(check_df)


@pytest.mark.parametrize('base_image', [
    "base:image",
    "different_registry.com/base:image",
])
def test_update_base_image(workflow, base_image):
    df_content = dedent("""\
        FROM {}
        LABEL horses=coconuts
        CMD whoah
    """)
    base_str = "base@sha256:1234"
    local_tag = ImageName.parse("base@sha256:1234")

    workflow = mock_workflow(workflow, df_content=df_content.format(base_image))
    workflow.data.dockerfile_images[base_image] = local_tag

    run_plugin(workflow)
    expected_df = df_content.format(base_str)

    check_df_content(expected_df, workflow)


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
def test_update_parent_images(df_content, expected_df_content, workflow):
    """test the happy path for updating multiple parents"""
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

    workflow = mock_workflow(workflow, df_content)

    dfp = workflow.build_dir.any_platform.dockerfile
    for parent in dfp.parent_images:
        if parent == 'scratch':
            continue
        parent_in = ImageName.parse(parent)
        workflow.data.dockerfile_images[parent] = parent_images[parent_in]

    original_base = workflow.data.dockerfile_images.base_image
    run_plugin(workflow)

    check_df_content(expected_df_content, workflow)

    if workflow.data.dockerfile_images.base_from_scratch:
        assert original_base == workflow.data.dockerfile_images.base_image


def test_parent_images_unresolved(workflow):
    """test when parent_images hasn't been filled in with unique tags."""
    df_content = dedent("""\
        FROM extra_image
        FROM base_image
    """)

    workflow = mock_workflow(workflow, df_content)
    workflow.data.dockerfile_images['base_image'] = 'base_local'

    with pytest.raises(PluginFailedException) as exc:
        run_plugin(workflow)
    assert "raised an exception: ParentImageUnresolved" in str(exc.value)


def test_parent_images_missing(workflow):
    """test when parent_images has been mangled and lacks parents compared to dockerfile."""
    df_content = dedent("""\
        FROM first:parent AS builder1
        FROM second:parent AS builder2
        FROM monty
    """)

    workflow = mock_workflow(workflow, df_content, override_df_images=['monty'])
    workflow.data.dockerfile_images['monty'] = 'monty_local'

    with pytest.raises(PluginFailedException) as exc:
        run_plugin(workflow)
    assert "raised an exception: ParentImageMissing" in str(exc.value)


def test_parent_images_mismatch_base_image(workflow):
    """test when base_image has been updated differently from parent_images."""
    df_content = "FROM base:image"
    workflow = mock_workflow(workflow,
                             df_content,
                             override_df_images=['base:image', 'parent_different:latest'])

    workflow.data.dockerfile_images['base:image'] = 'base:resolved'
    workflow.data.dockerfile_images['parent_different:latest'] = 'parent_different:resolved'

    with pytest.raises(PluginFailedException) as exc:
        run_plugin(workflow)

    assert "raised an exception: BaseImageMismatch" in str(exc.value)
