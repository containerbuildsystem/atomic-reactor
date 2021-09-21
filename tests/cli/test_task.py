"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import flexmock

from atomic_reactor.cli import task
from atomic_reactor.tasks import orchestrator, worker, sources, common

TASK_ARGS = {
    "build_dir": "/build",
    "context_dir": "/context",
    "config_file": "reactor-config-map.yaml",
    "user_params": '{"some_param": "some_value"}',
}

TASK_RESULT = object()


def mock(task_cls):
    params = flexmock()
    (
        #  mock the common TaskParams because child classes do not override from_cli_args
        flexmock(common.TaskParams)
        .should_receive("from_cli_args")
        .with_args(TASK_ARGS)
        .and_return(params)
    )
    flexmock(task_cls).should_receive("__init__").with_args(params)
    flexmock(task_cls).should_receive("execute").and_return(TASK_RESULT)


def test_orchestrator():
    mock(orchestrator.OrchestratorTask)
    assert task.orchestrator(TASK_ARGS) == TASK_RESULT


def test_worker():
    mock(worker.WorkerTask)
    assert task.worker(TASK_ARGS) == TASK_RESULT


def test_source_build():
    mock(sources.SourceBuildTask)
    assert task.source_build(TASK_ARGS) == TASK_RESULT
