"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

TASK_STATES = {
    'FREE': 0,
    'OPEN': 1,
    'CLOSED': 2,
    'CANCELED': 3,
    'ASSIGNED': 4,
    'FAILED': 5,
}

TASK_STATES.update({value: name for name, value in TASK_STATES.items()})

class ClientSession(object):
    def __init__(self, hub):
        raise ImportError("No module named koji")


class PathInfo(object):
    def __init__(self, topdir=None):
        raise ImportError("No module named koji")
