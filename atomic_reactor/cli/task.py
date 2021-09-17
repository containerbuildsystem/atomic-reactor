"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""


def orchestrator(task_args: dict):
    """Orchestrate a binary container build.

    :param task_args: CLI arguments for an orchestrator task
    """
    raise NotImplementedError("This task is not yet implemented")


def worker(task_args: dict):
    """Run the worker task for a binary container build.

    :param task_args: CLI arguments for a worker task
    """
    raise NotImplementedError("This task is not yet implemented")


def source_build(task_args: dict):
    """Run a source container build.

    :param task_args: CLI arguments for a source-build task
    """
    raise NotImplementedError("This task is not yet implemented")
