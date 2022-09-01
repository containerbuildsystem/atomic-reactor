"""
Copyright (c) 2021-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import argparse
from typing import Optional, Sequence

import pkg_resources

from atomic_reactor.constants import PROG, DESCRIPTION, REACTOR_CONFIG_FULL_PATH
from atomic_reactor.cli import task, job


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
    job_parser = subcommands.add_parser(
        "job",
        help="run a job",
        description="Run a specific job \
            (job is not intended to run as task in tekton pipeline but rather as cronjob)",
    )
    job_parser.add_argument(
        "--config-file",
        metavar="FILE",
        default=REACTOR_CONFIG_FULL_PATH,
        help=f"{PROG} configuration file",
    )
    job_parser.add_argument(
        "--namespace",
        help="name of namespace for job",
        required=True,
        metavar="NAMESPACE",
    )

    # The individual tasks
    tasks = task_parser.add_subparsers(title="tasks", metavar="task", required=True)
    jobs = job_parser.add_subparsers(title="jobs", metavar="job", required=True)

    source_container_build = tasks.add_parser(
        "source-container-build",
        help="build a source container",
        description="Build a source container.",
    )
    source_container_build.set_defaults(func=task.source_container_build)

    source_container_exit = tasks.add_parser(
        "source-container-exit",
        help="exit a source container build",
        description="Execute source container exit steps.",
    )
    source_container_exit.set_defaults(func=task.source_container_exit)
    source_container_exit.add_argument("--annotations-result", metavar="FILE", default=None,
                                       help="file to write annotations result")

    clone = tasks.add_parser(
        "clone",
        help="Clone source to build",
        description="Source is cloned by this command and used by rest of the build tasks",
    )
    clone.set_defaults(func=task.clone)

    binary_container_prebuild = tasks.add_parser(
        "binary-container-prebuild",
        help="binary container pre-build step",
        description="Execute binary container pre-build steps.",
    )
    binary_container_prebuild.set_defaults(func=task.binary_container_prebuild)
    binary_container_prebuild.add_argument("--platforms-result", metavar="FILE", default=None,
                                           help="file to write final platforms result")

    binary_container_build = tasks.add_parser(
        "binary-container-build",
        help="build a binary container",
        description="Build a binary container.",
    )
    binary_container_build.set_defaults(func=task.binary_container_build)
    binary_container_build.add_argument('--platform', action="store", required=True,
                                        help="platform on which to build container")

    binary_container_postbuild = tasks.add_parser(
        "binary-container-postbuild",
        help="binary container post-build step",
        description="Execute binary container post-build steps.",
    )
    binary_container_postbuild.set_defaults(func=task.binary_container_postbuild)

    binary_container_exit = tasks.add_parser(
        "binary-container-exit",
        help="exit a binary container build",
        description="Execute binary container exit steps.",
    )
    binary_container_exit.set_defaults(func=task.binary_container_exit)
    binary_container_exit.add_argument("--annotations-result", metavar="FILE", default=None,
                                       help="file to write annotations result")

    remote_hosts_unlocking_recovery = jobs.add_parser(
        "remote-hosts-unlocking-recovery",
        help="unlock remote hosts recovery",
        description="Unlock remote hosts recovery.",
    )
    remote_hosts_unlocking_recovery.set_defaults(func=job.remote_hosts_unlocking_recovery)

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
    task_parser.add_argument(
        '--namespace',
        metavar="STRING",
        required=True,
        help="OpenShift namespace of the task"
    )
    task_parser.add_argument(
        '--pipeline-run-name',
        metavar="STRING",
        required=True,
        help="PipelineRun name to reference current PipelineRun"
    )
    task_parser.add_argument(
        "--task-result",
        metavar="FILE",
        default=None,
        help="file to write task result",
    )
    # Two different ways to pass user params
    userparams_ex = task_parser.add_mutually_exclusive_group()
    userparams_ex.add_argument(
        "--user-params", metavar="JSON", help="JSON string with user configuration"
    )
    userparams_ex.add_argument(
        "--user-params-file", metavar="FILE", help="JSON file with user configuration"
    )
