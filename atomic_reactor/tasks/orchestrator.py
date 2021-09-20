"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from dataclasses import dataclass

from atomic_reactor.tasks import common


@dataclass(frozen=True)
class OrchestratorTaskParams(common.TaskParams):
    """Orchestrator task parameters (this task only uses common parameters)."""


class OrchestratorTask(common.Task):
    """Orchestrator task."""

    plugins_def = common.PluginsDef(
        prebuild=[
            {"name": "check_user_settings"},
            {"name": "check_and_set_platforms", "required": False},
            {"name": "flatpak_create_dockerfile"},
            {"name": "inject_parent_image"},
            {"name": "pull_base_image", "args": {"check_platforms": True, "inspect_only": True}},
            {"name": "koji_parent"},
            {"name": "resolve_composes"},
            {"name": "add_filesystem"},
            {"name": "flatpak_update_dockerfile"},
            {"name": "bump_release"},
            {"name": "add_flatpak_labels"},
            {"name": "add_labels_in_dockerfile"},
            {"name": "resolve_remote_source"},
            {"name": "pin_operator_digest"},
        ],
        build=[
            {"name": "orchestrate_build"},
        ],
        postbuild=[
            {"name": "fetch_worker_metadata"},
            {"name": "compare_components"},
            {"name": "tag_from_config", "args": {"tag_suffixes": "{{TAG_SUFFIXES}}"}},
            {"name": "group_manifests"},
            {"name": "generate_maven_metadata"},
        ],
        exit=[
            {"name": "verify_media", "required": False},
            {"name": "koji_import"},
            {"name": "push_floating_tags"},
            {"name": "koji_tag_build"},
            {"name": "store_metadata_in_osv3"},
            {"name": "sendmail"},
            {"name": "remove_built_image"},
            {"name": "remove_worker_metadata"},
        ],
    )

    def execute(self):
        raise NotImplementedError("This task is not yet implemented.")
