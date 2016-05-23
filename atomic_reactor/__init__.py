"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


constants
"""

import logging


__version__ = "1.6.8"


def set_logging(name="atomic_reactor", level=logging.DEBUG, handler=None):
    # create logger
    logger = logging.getLogger(name)
    logger.handlers = []
    logger.setLevel(level)

    if not handler:
        # create console handler and set level to debug
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)

        # create formatter
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

        # add formatter to ch
        handler.setFormatter(formatter)

    # add ch to logger
    logger.addHandler(handler)


set_logging(level=logging.WARNING)  # override this however you want
