"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.tasks import plugin_based


class BinaryPreBuildTask(plugin_based.PluginBasedTask):
    """Binary container pre-build task."""

    plugins_def = plugin_based.PluginsDef(
        prebuild=[
            {"name": "check_user_settings"},
            {"name": "check_and_set_platforms"},
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
            {"name": "distgit_fetch_artefacts"},
            {"name": "inject_yum_repos"},
            {"name": "hide_files"},
            {"name": "distribution_scope"},
            {"name": "add_buildargs_in_dockerfile"},
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
            {"name": "flatpak_create_oci"},
            {"name": "all_rpm_packages"},
            {"name": "tag_from_config"},
            {"name": "export_operator_manifests"},
            {"name": "compress"},
            {"name": "fetch_worker_metadata"},
            {"name": "compare_components"},
            {"name": "group_manifests"},
            {"name": "generate_maven_metadata"},
        ],
    )


class BinaryExitTask(plugin_based.PluginBasedTask):
    """Binary container exit-build task."""

    plugins_def = plugin_based.PluginsDef(
        exit=[
            {"name": "verify_media", "required": False},
            {"name": "koji_import"},
            {"name": "push_floating_tags"},
            {"name": "koji_tag_build"},
            {"name": "store_metadata"},
            {"name": "sendmail"},
        ],
    )
