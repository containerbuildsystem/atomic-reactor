"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import logging

from flexmock import flexmock
import osbs
import pytest

import atomic_reactor
from atomic_reactor.cli import main, parser, task


@pytest.mark.parametrize(
    "verbose, quiet, expect_loglevel",
    [
        (False, False, logging.INFO),
        (True, False, logging.DEBUG),
        (False, True, logging.WARNING),
    ],
)
def test_run(verbose, quiet, expect_loglevel):
    # the task should be called with task arguments only
    flexmock(task).should_receive("source_build").with_args({"user_params": "{}"})
    (
        flexmock(parser)
        .should_receive("parse_args")
        .and_return(
            # parse args will return global arguments mixed with task task arguments
            {"verbose": verbose, "quiet": quiet, "user_params": "{}", "func": task.source_build}
        )
    )
    flexmock(atomic_reactor).should_receive("set_logging").with_args(level=expect_loglevel)
    flexmock(osbs).should_receive("set_logging").with_args(level=expect_loglevel)
    main.run()
