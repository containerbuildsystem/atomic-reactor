"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.tasks import common
from atomic_reactor.tasks import PluginsDef

# PluginsDef can be considered as part of this module, but is defined elsewhere to avoid cyclic
#   imports between the `inner` module and this module
__all__ = ["PluginsDef", "PluginBasedTask"]


class PluginBasedTask(common.Task):
    """Task that executes a predefined list of plugins."""

    # Override this in subclasses
    plugins_def: PluginsDef = NotImplemented

    def execute(self):
        """Execute the plugins defined in plugins_def."""
        raise NotImplementedError("This task is not yet implemented")
