"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from atomic_reactor.tasks.binary import (BinaryExitTask, BinaryPostBuildTask, BinaryPreBuildTask,
                                         BinaryInitTask, BinaryCachitoTask,
                                         BinaryCachi2InitTask,
                                         InitTaskParams, BinaryExitTaskParams)
from atomic_reactor.tasks.binary_container_build import BinaryBuildTask, BinaryBuildTaskParams
from atomic_reactor.tasks.clone import CloneTask
from atomic_reactor.tasks.common import TaskParams
from atomic_reactor.tasks.sources import (SourceExitTask, SourceBuildTask, SourceBuildTaskParams,
                                          SourceExitTaskParams)


def source_container_build(task_args: dict):
    """Run a source container build.

    :param task_args: CLI arguments for a source-container-build task
    """
    params = SourceBuildTaskParams.from_cli_args(task_args)
    task = SourceBuildTask(params)
    return task.run()


def source_container_exit(task_args: dict):
    """Run source container exit steps.

    :param task_args: CLI arguments for a source-container-exit task
    """
    params = SourceExitTaskParams.from_cli_args(task_args)
    task = SourceExitTask(params)
    return task.run()


def clone(task_args: dict):
    """Clone source to build.

    :param task_args: CLI arguments for a clone task
    """
    params = TaskParams.from_cli_args(task_args)
    task = CloneTask(params)
    return task.run()


def binary_container_init(task_args: dict):
    """Run binary container pre-build steps.

    :param task_args: CLI arguments for a binary-container-init task
    """
    params = InitTaskParams.from_cli_args(task_args)
    task = BinaryInitTask(params)
    return task.run()


def binary_container_cachito(task_args: dict):
    """Run binary container Cachito steps.

    :param task_args: CLI arguments for a binary-container-cachito task
    """
    params = TaskParams.from_cli_args(task_args)
    task = BinaryCachitoTask(params)
    return task.run(init_build_dirs=True)


def binary_container_cachi2_init(task_args: dict):
    """Run binary container Cachi2 init step.

    :param task_args: CLI arguments for a binary-container-cachi2-init task
    """
    params = TaskParams.from_cli_args(task_args)
    task = BinaryCachi2InitTask(params)
    return task.run(init_build_dirs=True)


def binary_container_prebuild(task_args: dict):
    """Run binary container pre-build steps.

    :param task_args: CLI arguments for a binary-container-prebuild task
    """
    params = TaskParams.from_cli_args(task_args)
    task = BinaryPreBuildTask(params)
    return task.run(init_build_dirs=True)


def binary_container_build(task_args: dict):
    """Run a binary container build.

    :param task_args: CLI arguments for a binary-container-build task
    """
    params = BinaryBuildTaskParams.from_cli_args(task_args)
    task = BinaryBuildTask(params)
    return task.run()


def binary_container_postbuild(task_args: dict):
    """Run binary container post-build steps.

    :param task_args: CLI arguments for a binary-container-postbuild task
    """
    params = TaskParams.from_cli_args(task_args)
    task = BinaryPostBuildTask(params)
    return task.run(init_build_dirs=True)


def binary_container_exit(task_args: dict):
    """Run binary container exit steps.

    :param task_args: CLI arguments for a binary-container-exit task
    """
    params = BinaryExitTaskParams.from_cli_args(task_args)
    task = BinaryExitTask(params)
    return task.run(init_build_dirs=True)
