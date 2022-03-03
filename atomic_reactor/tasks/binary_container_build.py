"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import logging
from dataclasses import dataclass

from atomic_reactor.tasks.common import Task, TaskParams


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BinaryBuildTaskParams(TaskParams):
    """Binary container build task parameters"""
    platform: str


class BinaryBuildTask(Task):
    """Binary container build task."""

    def execute(self):
        logger.warning("This task doesn't do anything yet.")
