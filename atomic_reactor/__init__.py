"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


constants
"""

import logging
import time

from atomic_reactor.version import __version__

try:
    from osbs.constants import ATOMIC_REACTOR_LOGGING_FMT
except ImportError:
    # not available in osbs < 0.41
    fmt = '%(asctime)s platform:%(arch)s - %(name)s - %(levelname)s - %(message)s'
    ATOMIC_REACTOR_LOGGING_FMT = fmt

start_time = time.time()


class ArchFormatter(logging.Formatter):
    def format(self, record):
        if not hasattr(record, 'arch'):
            record.arch = '-'

        return super(ArchFormatter, self).format(record)


def set_logging(name="atomic_reactor", level=logging.DEBUG, handler=None):
    # create logger
    logger = logging.getLogger(name)
    for hdlr in list(logger.handlers):  # make a copy so it doesn't change
        logger.removeHandler(hdlr)

    logger.setLevel(level)

    if not handler:
        # create console handler and set level to debug
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)

        # create formatter
        formatter = ArchFormatter(ATOMIC_REACTOR_LOGGING_FMT)

        # add formatter to ch
        handler.setFormatter(formatter)

    # add ch to logger
    logger.addHandler(handler)


set_logging(level=logging.WARNING)  # override this however you want
