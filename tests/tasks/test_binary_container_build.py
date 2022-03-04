"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from pathlib import Path
from textwrap import dedent
from typing import Any, Dict, List

from osbs.utils import ImageName

import pytest
from flexmock import flexmock

from atomic_reactor import dirs
from atomic_reactor import inner
from atomic_reactor.constants import PLUGIN_CHECK_AND_SET_PLATFORMS_KEY
from atomic_reactor.tasks.binary_container_build import BinaryBuildTask, BinaryBuildTaskParams

CONTEXT_DIR = "/workspace/ws-context-dir"
CONFIG_PATH = "/etc/atomic-reactor/config.yaml"

NOARCH_UNIQUE_IMAGE = ImageName.parse("registry.example.org/osbs/spam:v1.0")
X86_UNIQUE_IMAGE = ImageName.parse("registry.example.org/osbs/spam:v1.0-x86_64")

BUILD_ARGS = {"REMOTE_SOURCES": "unpacked_remote_sources"}

DOCKERFILE_CONTENT = dedent(
    """\
    FROM fedora:35

    RUN echo "Hello there."
    """
)


@pytest.fixture
def base_task_params(build_dir: Path) -> Dict[str, Any]:
    return {
        "build_dir": str(build_dir),
        "context_dir": CONTEXT_DIR,
        "config_file": CONFIG_PATH,
        "user_params": {},
    }


@pytest.fixture
def x86_task_params(base_task_params) -> BinaryBuildTaskParams:
    return BinaryBuildTaskParams(**base_task_params, platform="x86_64")


@pytest.fixture
def aarch64_task_params(base_task_params) -> BinaryBuildTaskParams:
    return BinaryBuildTaskParams(**base_task_params, platform="aarch64")


@pytest.fixture
def x86_build_dir(build_dir: Path) -> dirs.BuildDir:
    x86_dir = build_dir / "x86_64"
    x86_dir.mkdir(exist_ok=True)
    return dirs.BuildDir(x86_dir, "x86_64")


def mock_workflow_data(*, enabled_platforms: List[str]) -> inner.ImageBuildWorkflowData:
    """Make load_workflow_data() return mocked workflow data. Also return this data."""
    tag_conf = inner.TagConf()
    tag_conf.add_unique_image(NOARCH_UNIQUE_IMAGE)

    mocked_data = inner.ImageBuildWorkflowData(
        tag_conf=tag_conf,
        prebuild_results={PLUGIN_CHECK_AND_SET_PLATFORMS_KEY: enabled_platforms},
        buildargs=BUILD_ARGS,
    )

    (
        flexmock(BinaryBuildTask)
        .should_receive("load_workflow_data")
        .and_return(mocked_data)
    )
    return mocked_data


class TestBinaryBuildTask:
    """Tests from the BinaryBuildTask class."""

    def test_platform_is_not_enabled(self, aarch64_task_params, caplog):
        mock_workflow_data(enabled_platforms=["x86_64"])
        flexmock(BinaryBuildTask).should_receive("build_container").never()

        task = BinaryBuildTask(aarch64_task_params)
        task.execute()

        assert "Platform aarch64 is not enabled for this build" in caplog.text

    def test_run_build(self, x86_task_params, x86_build_dir, caplog):
        mock_workflow_data(enabled_platforms=["x86_64"])
        x86_build_dir.dockerfile_path.write_text(DOCKERFILE_CONTENT)

        def mock_build_container(*, build_dir, build_args, dest_tag):
            assert build_dir.path == x86_build_dir.path
            assert build_dir.platform == "x86_64"
            assert build_args == BUILD_ARGS
            assert dest_tag == X86_UNIQUE_IMAGE

            yield from ["output line 1", "output line 2"]

        (
            flexmock(BinaryBuildTask)
            .should_receive("build_container")
            .once()
            .replace_with(mock_build_container)
        )
        (
            flexmock(BinaryBuildTask)
            .should_receive("push_container")
            .with_args(X86_UNIQUE_IMAGE)
            .once()
        )

        task = BinaryBuildTask(x86_task_params)
        task.execute()

        assert (
            f"Building for the x86_64 platform from {x86_build_dir.dockerfile_path}" in caplog.text
        )
        assert "output line 1" in caplog.text
        assert "output line 2" in caplog.text
        assert DOCKERFILE_CONTENT in caplog.text
