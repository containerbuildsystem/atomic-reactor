"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.tasks import common


class CloneTask(common.Task):
    """Clone task."""

    def execute(self):
        self._params.source.get()
