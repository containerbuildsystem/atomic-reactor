"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import re

import pytest

from atomic_reactor import constants
from atomic_reactor.cli import parser, task

BUILD_DIR = "/build"
CONTEXT_DIR = "/context"
REQUIRED_COMMON_ARGS = ["--build-dir", BUILD_DIR, "--context-dir", CONTEXT_DIR]

SOURCE_URI = "git://example.org/namespace/repo"

EXPECTED_ARGS = {
    "quiet": False,
    "verbose": False,
    "build_dir": BUILD_DIR,
    "context_dir": CONTEXT_DIR,
    "config_file": constants.REACTOR_CONFIG_FULL_PATH,
    "user_params": None,
    "user_params_file": None,
}


def test_parse_args_version(capsys):
    with pytest.raises(SystemExit):
        parser.parse_args(["--version"])

    stdout = capsys.readouterr().out
    assert re.match(r"^\d+\.\d+\.\d+$", stdout.strip())


@pytest.mark.parametrize(
    "cli_args, expect_parsed_args",
    [
        # required args only
        (
            ["task", *REQUIRED_COMMON_ARGS, "source-build"],
            {**EXPECTED_ARGS, "func": task.source_build},
        ),
        # global and required args
        (
            ["--verbose", "task", *REQUIRED_COMMON_ARGS, "orchestrator"],
            {**EXPECTED_ARGS, "verbose": True, "func": task.orchestrator},
        ),
        (
            ["--quiet", "task", *REQUIRED_COMMON_ARGS, "worker"],
            {**EXPECTED_ARGS, "quiet": True, "func": task.worker},
        ),
        # all common task args
        (
            ["task", *REQUIRED_COMMON_ARGS, "--config-file=config.yaml", "source-build"],
            {**EXPECTED_ARGS, "config_file": "config.yaml", "func": task.source_build},
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "--user-params", '{"a": 1}', "orchestrator"],
            {**EXPECTED_ARGS, "user_params": '{"a": 1}', "func": task.orchestrator},
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "--user-params-file", "params.json", "worker"],
            {**EXPECTED_ARGS, "user_params_file": "params.json", "func": task.worker},
        ),
    ],
)
def test_parse_args_valid(cli_args, expect_parsed_args):
    assert parser.parse_args(cli_args) == expect_parsed_args


@pytest.mark.parametrize(
    "cli_args, expect_error",
    [
        # missing subcommand
        ([], "the following arguments are required: subcommand"),
        # missing task
        (["task", *REQUIRED_COMMON_ARGS], "the following arguments are required: task"),
        # --verbose vs. --quiet
        (["--verbose", "--quiet"], "-q/--quiet: not allowed with argument -v/--verbose"),
        # --user-params vs. --user-params-file
        (
            ["task", *REQUIRED_COMMON_ARGS, "--user-params={}", "--user-params-file=up.json"],
            "--user-params-file: not allowed with argument --user-params",
        ),
        # args in the wrong place
        (
            ["task", *REQUIRED_COMMON_ARGS, "--verbose", "source-build"],
            "unrecognized arguments: --verbose",
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "source-build", "--user-params={}"],
            "unrecognized arguments: --user-params",
        ),
        # missing common arguments
        (
            ["task", "source-build"],
            "the following arguments are required: --build-dir, --context-dir",
        ),
    ],
)
def test_parse_args_invalid(cli_args, expect_error, capsys):
    with pytest.raises(SystemExit):
        parser.parse_args(cli_args)

    stderr = capsys.readouterr().err
    assert expect_error in stderr
