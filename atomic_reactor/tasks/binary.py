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
from dataclasses import dataclass

from atomic_reactor import inner
from atomic_reactor.constants import DOCKERFILE_FILENAME
from atomic_reactor.tasks import plugin_based
from atomic_reactor.tasks.common import TaskParams

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreBuildTaskParams(TaskParams):
    """Binary container prebuild task parameters"""
    platforms_result: Optional[str]


@dataclass(frozen=True)
class BinaryExitTaskParams(TaskParams):
    """Binary container exit task parameters"""
    annotations_result: Optional[str]


class BinaryPreBuildTask(plugin_based.PluginBasedTask[PreBuildTaskParams]):
    """Binary container pre-build task."""

    task_name = 'binary_container_prebuild'
    plugins_conf = [
        {"name": "distgit_fetch_artefacts"},
        {"name": "check_and_set_platforms"},
        {"name": "check_user_settings"},
        {"name": "flatpak_create_dockerfile"},
        {"name": "inject_parent_image"},
        {"name": "check_base_image"},
        {"name": "koji_parent"},
        {"name": "resolve_composes"},
        {"name": "flatpak_update_dockerfile"},
        {"name": "bump_release"},
        {"name": "add_flatpak_labels"},
        {"name": "add_labels_in_dockerfile"},
        {"name": "resolve_remote_source"},
        {"name": "pin_operator_digest"},
        {"name": "add_help"},
        {"name": "fetch_maven_artifacts"},
        {"name": "add_image_content_manifest"},
        {"name": "add_dockerfile"},
        {"name": "inject_yum_repos"},
        {"name": "add_filesystem"},
        {"name": "change_from_in_dockerfile"},
        {"name": "hide_files"},
        {"name": "distribution_scope"},
        {"name": "add_buildargs_in_dockerfile"},
        {"name": "tag_from_config"},
    ]

    def prepare_workflow(self) -> inner.DockerBuildWorkflow:
        """Fully initialize the workflow instance to be used for running the list of plugins."""
        workflow = inner.DockerBuildWorkflow(
            context_dir=self.get_context_dir(),
            build_dir=self.get_build_dir(),
            data=self.workflow_data,
            namespace=self._params.namespace,
            pipeline_run_name=self._params.pipeline_run_name,
            source=self._params.source,
            user_params=self._params.user_params,
            reactor_config_path=self._params.config_file,
            # Set what plugins to run and how
            plugins_conf=self.plugins_conf,
            keep_plugins_running=self.keep_plugins_running,
            platforms_result=self._params.platforms_result,
        )
        return workflow


class BinaryPostBuildTask(plugin_based.PluginBasedTask[TaskParams]):
    """Binary container post-build task."""

    task_name = 'binary_container_postbuild'
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
        {"name": "generate_sbom"},
        {"name": "koji_import"},
        {"name": "koji_tag_build"},
    ]


class BinaryExitTask(plugin_based.PluginBasedTask[BinaryExitTaskParams]):
    """Binary container exit-build task."""

    task_name = 'binary_container_exit'
    keep_plugins_running = True
    ignore_sigterm = True
    plugins_conf = [
        {"name": "cancel_build_reservation"},
        {"name": "store_metadata"},
        {"name": "sendmail"},
    ]

    def prepare_workflow(self) -> inner.DockerBuildWorkflow:
        """Fully initialize the workflow instance to be used for running the list of plugins."""
        workflow = super().prepare_workflow()
        workflow.annotations_result = self._params.annotations_result
        return workflow

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
