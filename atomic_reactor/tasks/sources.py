"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from dataclasses import dataclass
from typing import ClassVar

from atomic_reactor.tasks import common


@dataclass(frozen=True)
class SourceBuildTaskParams(common.TaskParams):
    """Source build task parameters (this task only uses common parameters)."""

    # Validate with the source containers schema instead
    user_params_schema: ClassVar[str] = "schemas/source_containers_user_params.json"


class SourceBuildTask(common.Task):
    """Source container build task."""

    plugins_def = common.PluginsDef(
        prebuild=[
            {"name": "fetch_sources"},
            {"name": "bump_release"},
        ],
        build=[
            {"name": "source_container"},
        ],
        postbuild=[
            {"name": "compress", "args": {"load_exported_image": True, "method": "gzip"}},
            {"name": "tag_and_push"},
        ],
        exit=[
            {"name": "verify_media", "required": False},
            {"name": "koji_import_source_container"},
            {"name": "koji_tag_build"},
            {"name": "store_metadata_in_osv3"},
        ],
    )

    def execute(self):
        raise NotImplementedError("This task is not yet implemented.")
