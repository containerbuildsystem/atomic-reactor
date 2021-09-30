"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import abc
from dataclasses import dataclass, fields
from typing import Dict, Any, ClassVar

from atomic_reactor import util


@dataclass(frozen=True)
class TaskParams:
    """Task parameters (coming from CLI arguments)."""

    user_params_schema: ClassVar[str] = "schemas/user_params.json"

    build_dir: str
    context_dir: str
    config_file: str
    user_params: Dict[str, Any]

    # Note: do not give any attributes in this class default values, that would make dataclass
    #   inheritance difficult. If they should have defaults, define them in the CLI parser.

    @classmethod
    def from_cli_args(cls, args: dict):
        """Create a TaskParams instance from CLI arguments."""
        args = cls._drop_known_unset_args(args)
        params_str = args.pop("user_params", None)
        params_file = args.pop("user_params_file", None)

        if params_str:
            user_params = util.read_yaml(params_str, cls.user_params_schema)
        elif params_file:
            user_params = util.read_yaml_from_file_path(params_file, cls.user_params_schema)
        else:
            raise ValueError("Did not receive user params. User params are currently required.")

        return cls(**args, user_params=user_params)

    @classmethod
    def _drop_known_unset_args(cls, args: dict) -> dict:
        # When an argument is not set on the CLI, argparse stores it as None. Drop those arguments
        #   to avoid accidentally setting required attributes to None, make sure we instead get
        #   a TypeError from __init__().
        # Drop only arguments defined on this class (or a parent class), if an unknown argument
        #   is received, we want a TypeError regardless of the value.
        # The CLI should be responsible for not letting any of this happen, but let's double-check.
        known_args = {f.name for f in fields(cls)}
        return {k: v for k, v in args.items() if v is not None or k not in known_args}


class Task(abc.ABC):
    """Task; the main execution unit in atomic-reactor."""

    def __init__(self, params: TaskParams):
        """Initialize a Task."""
        self._params = params

    @abc.abstractmethod
    def execute(self):
        """Execute this task."""
