"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from dataclasses import dataclass
from atomic_reactor.tasks import plugin_based
from atomic_reactor.tasks.common import TaskParams


@dataclass(frozen=True)
class BinaryBuildTaskParams(TaskParams):
    """Binary container build task parameters"""
    platform: str


class BinaryPreBuildTask(plugin_based.PluginBasedTask):
    """Binary container pre-build task."""

    plugins_def = plugin_based.PluginsDef(
        prebuild=[
            {"name": "distgit_fetch_artefacts"},
            {"name": "check_and_set_platforms"},
            {"name": "check_user_settings"},
            {"name": "flatpak_create_dockerfile"},
            {"name": "inject_parent_image"},
            {"name": "check_base_image"},
            {"name": "koji_parent"},
            {"name": "resolve_composes"},
            {"name": "add_filesystem"},
            {"name": "flatpak_update_dockerfile"},
            {"name": "bump_release"},
            {"name": "add_labels_in_dockerfile"},
            {"name": "resolve_remote_source"},
            {"name": "pin_operator_digest"},
            {"name": "change_from_in_dockerfile"},
            {"name": "add_help"},
            {"name": "fetch_maven_artifacts"},
            {"name": "add_image_content_manifest"},
            {"name": "add_dockerfile"},
            {"name": "inject_yum_repos"},
            {"name": "hide_files"},
            {"name": "distribution_scope"},
            {"name": "add_buildargs_in_dockerfile"},
            {"name": "tag_from_config"},
        ],
    )


class BinaryBuildTask(plugin_based.PluginBasedTask):
    """Binary container build task."""

    plugins_def = plugin_based.PluginsDef(
        buildstep=[
            {"name": "binary_container"},
        ],
    )


class BinaryPostBuildTask(plugin_based.PluginBasedTask):
    """Binary container post-build task."""

    plugins_def = plugin_based.PluginsDef(
        postbuild=[
            {"name": "fetch_docker_archive"},
            {"name": "flatpak_create_oci"},
            {"name": "all_rpm_packages"},
            {"name": "export_operator_manifests"},
            {"name": "fetch_worker_metadata"},
            {"name": "compare_components"},
            {"name": "group_manifests"},
            {"name": "generate_maven_metadata"},
            {"name": "verify_media", "required": False},
            {"name": "push_floating_tags"},
            {"name": "koji_import"},
            {"name": "koji_tag_build"},
        ],
    )


class BinaryExitTask(plugin_based.PluginBasedTask):
    """Binary container exit-build task."""

    plugins_def = plugin_based.PluginsDef(
        exit=[
            {"name": "cancel_build_reservation"},
            {"name": "store_metadata"},
            {"name": "sendmail"},
        ],
    )
