"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from atomic_reactor.tasks.binary import BinaryBuildTask, BinaryExitTask, \
    BinaryPostBuildTask, BinaryPreBuildTask, BinaryBuildTaskParams
from atomic_reactor.tasks.clone import CloneTask
from atomic_reactor.tasks.common import TaskParams
from atomic_reactor.tasks.orchestrator import OrchestratorTask, OrchestratorTaskParams
from atomic_reactor.tasks.sources import SourceBuildTask, SourceBuildTaskParams
from atomic_reactor.tasks.worker import WorkerTask, WorkerTaskParams


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


def clone(task_args: dict):
    """Clone source to build.

    :param task_args: CLI arguments for a clone task
    """
    params = TaskParams.from_cli_args(task_args)
    task = CloneTask(params)
    return task.execute()


def binary_container_prebuild(task_args: dict):
    """Run binary container pre-build steps.

    :param task_args: CLI arguments for a binary-container-prebuild task
    """
    params = TaskParams.from_cli_args(task_args)
    task = BinaryPreBuildTask(params)
    return task.execute()


def binary_container_build(task_args: dict):
    """Run a binary container build.

    :param task_args: CLI arguments for a binary-container-build task
    """
    params = BinaryBuildTaskParams.from_cli_args(task_args)
    task = BinaryBuildTask(params)
    return task.execute()


def binary_container_postbuild(task_args: dict):
    """Run binary container post-build steps.

    :param task_args: CLI arguments for a binary-container-postbuild task
    """
    params = TaskParams.from_cli_args(task_args)
    task = BinaryPostBuildTask(params)
    return task.execute()


def binary_container_exit(task_args: dict):
    """Run binary container exit steps.

    :param task_args: CLI arguments for a binary-container-exit task
    """
    params = TaskParams.from_cli_args(task_args)
    task = BinaryExitTask(params)
    return task.execute()
