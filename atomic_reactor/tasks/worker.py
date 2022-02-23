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
class WorkerTaskParams(common.TaskParams):
    """Worker task parameters (this task only uses common parameters)."""


class WorkerTask(plugin_based.PluginBasedTask):
    """Worker task."""

    plugins_def = plugin_based.PluginsDef(
        prebuild=[
            {"name": "flatpak_create_dockerfile"},
            {"name": "flatpak_update_dockerfile"},
            {"name": "add_filesystem"},
            {"name": "inject_parent_image"},
            {"name": "check_base_image"},
            {"name": "add_flatpak_labels"},
            {"name": "add_labels_in_dockerfile"},
            {"name": "change_from_in_dockerfile"},
            {"name": "add_help"},
            {"name": "fetch_maven_artifacts"},
            {"name": "add_image_content_manifest"},
            {"name": "add_dockerfile"},
            {"name": "distgit_fetch_artefacts"},
            {"name": "koji"},
            {"name": "add_yum_repo_by_url"},
            {"name": "inject_yum_repo"},
            {"name": "hide_files"},
            {"name": "distribution_scope"},
            {"name": "download_remote_source"},
            {"name": "add_buildargs_in_dockerfile"},
            {"name": "pin_operator_digest"},
            {"name": "tag_from_config"},
        ],
        prepublish=[
            {"name": "squash"},
            {"name": "flatpak_create_oci"},
        ],
        postbuild=[
            {"name": "all_rpm_packages", "args": {"image_id": "BUILT_IMAGE_ID"}},
            {"name": "tag_and_push"},
            {"name": "export_operator_manifests"},
            {"name": "fetch_docker_archive", "args": {"load_exported_image": True, "method": "gzip"}},
            {"name": "koji_upload", "args": {"blocksize": 10485760}},
        ],
        exit=[
            {"name": "store_metadata"},
            {"name": "remove_built_image"},
        ],
    )
