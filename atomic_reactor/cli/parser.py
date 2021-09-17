"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import argparse
from typing import Optional, Sequence

import pkg_resources

from atomic_reactor.constants import PROG, DESCRIPTION, REACTOR_CONFIG_FULL_PATH
from atomic_reactor.cli import task


def parse_args(args: Optional[Sequence[str]] = None) -> dict:
    """Parse atomic-reactor CLI arguments.

    :param args: iterable of strings to parse as CLI arguments. By default, sys.argv[1:]
    :return: parsed arguments as a dict
    """
    parser = argparse.ArgumentParser(prog=PROG, description=DESCRIPTION)
    _add_global_args(parser)

    # Subcommands (there is only one - 'task')
    subcommands = parser.add_subparsers(title="subcommands", metavar="subcommand", required=True)
    task_parser = subcommands.add_parser(
        "task",
        help="run a task",
        description="Run a specific task in the container build process.",
    )
    _add_common_task_args(task_parser)

    # The individual tasks
    tasks = task_parser.add_subparsers(title="tasks", metavar="task", required=True)

    orchestrator = tasks.add_parser(
        "orchestrator",
        help="orchestrate a build",
        description="Orchestrate a binary container build.",
    )
    orchestrator.set_defaults(func=task.orchestrator)

    worker = tasks.add_parser(
        "worker",
        help="run the worker task",
        description="Run the worker task for a binary container build.",
    )
    worker.set_defaults(func=task.worker)

    source_build = tasks.add_parser(
        "source-build",
        help="build a source container",
        description="Build a source container.",
    )
    source_build.set_defaults(func=task.source_build)

    return vars(parser.parse_args(args))


def _add_global_args(parser: argparse.ArgumentParser) -> None:
    """Add global arguments to the main parser."""
    try:
        version = pkg_resources.get_distribution("atomic_reactor").version
    except pkg_resources.DistributionNotFound:
        version = "GIT"

    # -V/--version prints version info and exits the program
    parser.add_argument("-V", "--version", action="version", version=version)
    # -q/--quiet and -v/--verbose are exclusive for obvious reasons
    verbosity_ex = parser.add_mutually_exclusive_group()
    verbosity_ex.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress normal output, show only warnings and errors",
    )
    verbosity_ex.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="be more verbose, include debug messages in output",
    )


def _add_common_task_args(task_parser: argparse.ArgumentParser) -> None:
    """Add arguments common to all tasks to the task subparser."""
    task_parser.add_argument(
        "--build-dir",
        metavar="DIR",
        required=True,
        help="directory for the build input files",
    )
    task_parser.add_argument(
        "--context-dir",
        metavar="DIR",
        required=True,
        help="shared working directory for tasks",
    )
    task_parser.add_argument(
        "--config-file",
        metavar="FILE",
        default=REACTOR_CONFIG_FULL_PATH,
        help=f"{PROG} configuration file",
    )
    # Two different ways to pass user params
    userparams_ex = task_parser.add_mutually_exclusive_group()
    userparams_ex.add_argument(
        "--user-params", metavar="JSON", help="JSON string with user configuration"
    )
    userparams_ex.add_argument(
        "--user-params-file", metavar="FILE", help="JSON file with user configuration"
    )
