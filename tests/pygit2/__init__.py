"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""


def init_repository(name, **kwargs):
    raise ImportError("No module named pygit2")


class Remote(object):
    def __init__(self):
        raise ImportError("No module named pygit2")

    def push(self, name):
        raise ImportError("No module named pygit2")


class Signature(object):
    def __init__(self, name, email):
        raise ImportError("No module named pygit2")
