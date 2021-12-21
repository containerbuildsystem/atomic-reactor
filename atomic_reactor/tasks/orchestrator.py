"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from dataclasses import dataclass

from atomic_reactor.tasks import common
from atomic_reactor.tasks import plugin_based


@dataclass(frozen=True)
class OrchestratorTaskParams(common.TaskParams):
    """Orchestrator task parameters (this task only uses common parameters)."""


class OrchestratorTask(plugin_based.PluginBasedTask):
    """Orchestrator task."""

    plugins_def = plugin_based.PluginsDef(
        prebuild=[
            {"name": "check_user_settings"},
            {"name": "check_and_set_platforms", "required": False},
            {"name": "flatpak_create_dockerfile"},
            {"name": "inject_parent_image"},
            {"name": "check_base_image"},
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
        buildstep=[
            {"name": "orchestrate_build"},
        ],
        postbuild=[
            {"name": "fetch_worker_metadata"},
            {"name": "compare_components"},
            {"name": "tag_from_config"},
            {"name": "group_manifests"},
            {"name": "generate_maven_metadata"},
        ],
        exit=[
            {"name": "verify_media", "required": False},
            {"name": "koji_import"},
            {"name": "push_floating_tags"},
            {"name": "koji_tag_build"},
            {"name": "store_metadata"},
            {"name": "sendmail"},
            {"name": "remove_built_image"},
            {"name": "remove_worker_metadata"},
        ],
    )
