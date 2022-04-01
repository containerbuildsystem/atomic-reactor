"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from flexmock import flexmock

from atomic_reactor.cli import task
from atomic_reactor.tasks import (
    sources,
    common,
    binary,
    binary_container_build,
)
TASK_ARGS = {
    "build_dir": "/build",
    "context_dir": "/context",
    "config_file": "reactor-config-map.yaml",
    "user_params": '{"some_param": "some_value"}',
}

TASK_RESULT = object()


def mock(task_cls, init_build_dirs=False):
    params = flexmock()
    (
        #  mock the common TaskParams because child classes do not override from_cli_args
        flexmock(common.TaskParams)
        .should_receive("from_cli_args")
        .with_args(TASK_ARGS)
        .and_return(params)
    )
    flexmock(task_cls).should_receive("__init__").with_args(params)
    if init_build_dirs:
        (flexmock(task_cls)
         .should_receive("execute")
         .with_args(init_build_dirs=True)
         .and_return(TASK_RESULT))
    else:
        flexmock(task_cls).should_receive("execute").and_return(TASK_RESULT)


def test_source_build():
    mock(sources.SourceBuildTask)
    assert task.source_container_build(TASK_ARGS) == TASK_RESULT


def test_binary_container_prebuild():
    mock(binary.BinaryPreBuildTask)
    assert task.binary_container_prebuild(TASK_ARGS) == TASK_RESULT


def test_binary_container_build():
    mock(binary_container_build.BinaryBuildTask)
    assert task.binary_container_build(TASK_ARGS) == TASK_RESULT


def test_binary_container_postbuild():
    mock(binary.BinaryPostBuildTask, init_build_dirs=True)
    assert task.binary_container_postbuild(TASK_ARGS) == TASK_RESULT


def test_binary_container_exit():
    mock(binary.BinaryExitTask)
    assert task.binary_container_exit(TASK_ARGS) == TASK_RESULT
