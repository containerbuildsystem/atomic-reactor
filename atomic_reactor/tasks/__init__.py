"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from dataclasses import dataclass, field
from typing import List

from atomic_reactor import util


@dataclass(frozen=True)
class PluginsDef:
    """Defines the plugins to be executed by a task."""

    prebuild: List[dict] = field(default_factory=list)
    buildstep: List[dict] = field(default_factory=list)
    prepublish: List[dict] = field(default_factory=list)
    postbuild: List[dict] = field(default_factory=list)
    exit: List[dict] = field(default_factory=list)

    def __post_init__(self):
        """Validate the plugin definition right after the instance is created."""
        to_validate = {
            "prebuild_plugins": self.prebuild,
            "buildstep_plugins": self.buildstep,
            "prepublish_plugins": self.prepublish,
            "postbuild_plugins": self.postbuild,
            "exit_plugins": self.exit,
        }
        util.validate_with_schema(to_validate, "schemas/plugins.json")
