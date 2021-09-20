"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.tasks.orchestrator import OrchestratorTask, OrchestratorTaskParams
from atomic_reactor.tasks.worker import WorkerTask, WorkerTaskParams
from atomic_reactor.tasks.sources import SourceBuildTask, SourceBuildTaskParams


def orchestrator(task_args: dict):
    """Orchestrate a binary container build.

    :param task_args: CLI arguments for an orchestrator task
    """
    params = OrchestratorTaskParams.from_cli_args(task_args)
    task = OrchestratorTask(params)
    return task.execute()


def worker(task_args: dict):
    """Run the worker task for a binary container build.

    :param task_args: CLI arguments for a worker task
    """
    params = WorkerTaskParams.from_cli_args(task_args)
    task = WorkerTask(params)
    return task.execute()


def source_build(task_args: dict):
    """Run a source container build.

    :param task_args: CLI arguments for a source-build task
    """
    params = SourceBuildTaskParams.from_cli_args(task_args)
    task = SourceBuildTask(params)
    return task.execute()
