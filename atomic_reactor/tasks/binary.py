"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import logging
import os.path
from pathlib import Path
from typing import Optional

from atomic_reactor.constants import DOCKERFILE_FILENAME
from atomic_reactor.tasks import plugin_based
from atomic_reactor.tasks.common import TaskParams

logger = logging.getLogger(__name__)


class BinaryPreBuildTask(plugin_based.PluginBasedTask[TaskParams]):
    """Binary container pre-build task."""

    plugins_conf = [
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
    ]


class BinaryPostBuildTask(plugin_based.PluginBasedTask[TaskParams]):
    """Binary container post-build task."""

    plugins_conf = [
        {"name": "fetch_docker_archive"},
        {"name": "flatpak_create_oci"},
        {"name": "tag_and_push"},
        {"name": "all_rpm_packages"},
        {"name": "export_operator_manifests"},
        {"name": "gather_builds_metadata"},
        {"name": "compare_components"},
        {"name": "group_manifests"},
        {"name": "maven_url_sources_metadata"},
        {"name": "verify_media", "required": False},
        {"name": "push_floating_tags"},
        {"name": "koji_import"},
        {"name": "koji_tag_build"},
    ]


class BinaryExitTask(plugin_based.PluginBasedTask[TaskParams]):
    """Binary container exit-build task."""

    keep_plugins_running = True
    plugins_conf = [
        {"name": "cancel_build_reservation"},
        {"name": "store_metadata"},
        {"name": "sendmail"},
    ]

    def _output_dockerfile(self):
        dockerfile = Path(os.path.join(self._params.source.path, DOCKERFILE_FILENAME))
        if dockerfile.exists():
            logger.debug("Original Dockerfile:\n%s", dockerfile.read_text("utf-8"))
        else:
            logger.debug("No Dockerfile exists: %s", str(dockerfile))

    def execute(self, init_build_dirs: Optional[bool] = False):
        try:
            super(BinaryExitTask, self).execute(init_build_dirs)
        finally:
            self._output_dockerfile()
