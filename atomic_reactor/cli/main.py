"""
Copyright (c) 2015, 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import locale
import logging
import sys

import osbs

import atomic_reactor
from atomic_reactor.cli import parser
from atomic_reactor.util import setup_introspection_signal_handler


def _process_global_args(args: dict) -> dict:
    """Process global arguments, return non-global arguments (task arguments)."""
    task_args = args.copy()

    verbose = task_args.pop("verbose")
    quiet = task_args.pop("quiet")
    # Note: the version argument is not stored by argparse (because it has the 'version' action)

    if verbose:
        atomic_reactor.set_logging(level=logging.DEBUG)
        osbs.set_logging(level=logging.DEBUG)
    elif quiet:
        atomic_reactor.set_logging(level=logging.WARNING)
        osbs.set_logging(level=logging.WARNING)
    else:
        atomic_reactor.set_logging(level=logging.INFO)
        osbs.set_logging(level=logging.INFO)

    return task_args


def run():
    """Run atomic-reactor."""
    locale.setlocale(locale.LC_ALL, '')
    logging.captureWarnings(True)
    setup_introspection_signal_handler()

    args = parser.parse_args()
    task = args.pop("func")
    task_args = _process_global_args(args)

    return task(task_args)


if __name__ == '__main__':
    sys.exit(run())
