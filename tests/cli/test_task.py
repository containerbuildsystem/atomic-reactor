"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import json
import os

import pytest
from flexmock import flexmock

from atomic_reactor.cli import task
from atomic_reactor.constants import DOCKERFILE_FILENAME
from atomic_reactor.source import GitSource
from atomic_reactor.tasks import (
    sources,
    common,
    binary,
    binary_container_build,
)
from atomic_reactor.tasks.plugin_based import PluginBasedTask

TASK_ARGS = {
    "build_dir": "/build",
    "context_dir": "/context",
    "config_file": "reactor-config-map.yaml",
    "user_params": '{"some_param": "some_value"}',
}
PRE_TASK_ARGS = {
    **TASK_ARGS,
    "platforms_result": 'platform_result',
}

TASK_RESULT = object()


def mock(task_cls, init_build_dirs=False, task_args=TASK_ARGS):
    params = flexmock()
    (
        #  mock the common TaskParams because child classes do not override from_cli_args
        flexmock(common.TaskParams)
        .should_receive("from_cli_args")
        .with_args(task_args)
        .and_return(params)
    )
    flexmock(task_cls).should_receive("__init__").with_args(params)
    if init_build_dirs:
        (flexmock(task_cls)
         .should_receive("run")
         .with_args(init_build_dirs=True)
         .and_return(TASK_RESULT))
    else:
        flexmock(task_cls).should_receive("run").and_return(TASK_RESULT)


def test_source_build():
    mock(sources.SourceBuildTask)
    assert task.source_container_build(TASK_ARGS) == TASK_RESULT


def test_binary_container_prebuild():
    mock(binary.BinaryPreBuildTask, task_args=PRE_TASK_ARGS)
    assert task.binary_container_prebuild(PRE_TASK_ARGS) == TASK_RESULT


def test_binary_container_build():
    mock(binary_container_build.BinaryBuildTask)
    assert task.binary_container_build(TASK_ARGS) == TASK_RESULT


def test_binary_container_postbuild():
    mock(binary.BinaryPostBuildTask, init_build_dirs=True)
    assert task.binary_container_postbuild(TASK_ARGS) == TASK_RESULT


@pytest.mark.parametrize("has_dockerfile", [True, False])
def test_binary_container_exit(has_dockerfile, build_dir, context_dir, caplog):
    git_uri = "https://git.host/containers/coolapp"
    dockerfile_content = 'FROM fedora:36\nCMD ["exit", "0"]'

    if has_dockerfile:
        source = GitSource("git", git_uri, workdir=str(build_dir))
        os.mkdir(source.path)
        dockerfile = os.path.join(source.path, DOCKERFILE_FILENAME)
        with open(dockerfile, "w", encoding="utf-8") as f:
            f.write(dockerfile_content)

    (flexmock(PluginBasedTask)
     .should_receive("execute")
     .with_args(True)
     .and_return(TASK_RESULT))

    task_args = TASK_ARGS.copy()
    task_args["annotations_result"] = "annotations"
    task_args["context_dir"] = str(context_dir)
    task_args["build_dir"] = str(build_dir)
    task_args["namespace"] = "test"
    task_args["pipeline_run_name"] = "test-pr"
    task_args["user_params"] = json.dumps({
        "git_uri": git_uri, "git_ref": "1234", "user": "osbs"
    })
    task_args["task_result"] = None

    assert task.binary_container_exit(task_args) is None

    if has_dockerfile:
        assert f"Original Dockerfile:\n{dockerfile_content}" in caplog.text
    else:
        assert "No Dockerfile" in caplog.text
