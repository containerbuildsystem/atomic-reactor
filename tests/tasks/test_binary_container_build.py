"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import io
import re
import subprocess
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict, List, Optional

from osbs.utils import ImageName

import pytest
from flexmock import flexmock

from atomic_reactor import dirs
from atomic_reactor import inner
from atomic_reactor.constants import PLUGIN_CHECK_AND_SET_PLATFORMS_KEY
from atomic_reactor.tasks.binary_container_build import (
    BinaryBuildTask,
    BinaryBuildTaskParams,
    BuildProcessError,
    PushError,
)
from atomic_reactor.utils import retries

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


class MockedPopen:
    def __init__(self, rc: int, output_lines: List[str]):
        self._rc = rc
        self.stdout = io.StringIO("".join(output_lines))

    def poll(self):
        if self.stdout.tell() >= len(self.stdout.getvalue()):
            # no more output, the process has ended
            return self._rc
        return None


def mock_popen(
    rc: int, output_lines: List[str], expect_cmd: Optional[List[str]] = None
) -> None:
    """Make subprocess.Popen returned a mocked Popen.

    The output_lines should end with '\n', otherwise they wil be combined to a single line.
    Optionally, pass in the expected command to be checked.
    """
    def popen(cmd, *args, **kwargs):
        if expect_cmd:
            assert cmd == expect_cmd
        return MockedPopen(rc, output_lines)

    flexmock(subprocess).should_receive("Popen").replace_with(popen)


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

    def test_print_dockerfile_on_failure(self, x86_task_params, x86_build_dir, caplog):
        mock_workflow_data(enabled_platforms=["x86_64"])
        x86_build_dir.dockerfile_path.write_text(DOCKERFILE_CONTENT)

        (
            flexmock(BinaryBuildTask)
            .should_receive("build_container")
            .and_raise(BuildProcessError("something went wrong"))
        )

        task = BinaryBuildTask(x86_task_params)
        with pytest.raises(BuildProcessError):
            task.execute()

        assert DOCKERFILE_CONTENT in caplog.text

    def test_build_container(self, x86_task_params, x86_build_dir):
        # TBD: add the actual expect_cmd later
        mock_popen(0, ["starting the build\n", "finished successfully\n"], expect_cmd=None)

        task = BinaryBuildTask(x86_task_params)
        output_lines = task.build_container(
            build_dir=x86_build_dir,
            build_args=BUILD_ARGS,
            dest_tag=X86_UNIQUE_IMAGE,
        )

        assert list(output_lines) == ["starting the build\n", "finished successfully\n"]

    @pytest.mark.parametrize(
        "output_lines, expect_err_line",
        [
            (["starting the build\n", "failed :(\n"], "failed :("),
            ([], "<no output!>"),
        ]
    )
    def test_build_container_fails(
        self, output_lines, expect_err_line, x86_task_params, x86_build_dir
    ):
        mock_popen(1, output_lines)

        task = BinaryBuildTask(x86_task_params)
        returned_lines = task.build_container(
            build_dir=x86_build_dir,
            build_args=BUILD_ARGS,
            dest_tag=X86_UNIQUE_IMAGE,
        )

        for expect_line in output_lines:
            assert next(returned_lines) == expect_line

        err_msg = rf"Build failed \(rc=1\). {re.escape(expect_err_line)}"

        with pytest.raises(BuildProcessError, match=err_msg):
            next(returned_lines)

    def test_push_container(self, x86_task_params):
        (
            flexmock(retries)
            .should_receive("run_cmd")
            .with_args(["echo", str(X86_UNIQUE_IMAGE)])  # TBD: change me later
            .once()
        )

        task = BinaryBuildTask(x86_task_params)
        task.push_container(X86_UNIQUE_IMAGE)

    def test_push_container_fails(self, x86_task_params):
        (
            flexmock(retries)
            .should_receive("run_cmd")
            .and_raise(subprocess.CalledProcessError(1, 'some command'))
        )

        task = BinaryBuildTask(x86_task_params)

        err_msg = r"Push failed \(rc=1\). Check the logs for more details."

        with pytest.raises(PushError, match=err_msg):
            task.push_container(X86_UNIQUE_IMAGE)
