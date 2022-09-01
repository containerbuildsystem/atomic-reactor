"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from dataclasses import dataclass
from typing import ClassVar, Optional

from atomic_reactor import inner
from atomic_reactor import source
from atomic_reactor.tasks import common
from atomic_reactor.tasks import plugin_based


@dataclass(frozen=True)
class SourceBuildTaskParams(common.TaskParams):
    """Source build task parameters (this task only uses common parameters)."""

    # Validate with the source containers schema instead
    user_params_schema: ClassVar[str] = "schemas/source_containers_user_params.json"

    @property
    def source(self) -> source.Source:
        """Return a dummy source for a source container build."""
        return source.DummySource(None, None, workdir=self.build_dir)


@dataclass(frozen=True)
class SourceExitTaskParams(SourceBuildTaskParams):
    """Source exit task parameters"""
    annotations_result: Optional[str]


class SourceBuildBaseTask(plugin_based.PluginBasedTask[common.ParamsT]):
    """Base task for defining different build phases for source container build."""

    def prepare_workflow(self) -> inner.DockerBuildWorkflow:
        """After preparing the workflow as usual, fully initialize the root build dir.

        Unlike the binary container container workflow, the platforms to be used for the build
        are known in advance here (more accurately, source containers do not have a platform).
        """
        workflow = super().prepare_workflow()
        workflow.build_dir.init_build_dirs(["noarch"], self._params.source)
        return workflow


class SourceBuildTask(SourceBuildBaseTask):
    """Source container build phases task."""

    plugins_conf = [
        {"name": "fetch_sources"},
        {"name": "bump_release"},
        {"name": "source_container"},
        {"name": "tag_and_push"},
        {"name": "verify_media", "required": False},
        {"name": "koji_import_source_container"},
        {"name": "koji_tag_build"},
    ]


class SourceExitTask(SourceBuildBaseTask):
    """Source container exit task."""

    keep_plugins_running = True
    ignore_sigterm = True
    plugins_conf = [
        {"name": "cancel_build_reservation"},
        {"name": "store_metadata"},
    ]

    def prepare_workflow(self) -> inner.DockerBuildWorkflow:
        workflow = super().prepare_workflow()
        workflow.annotations_result = self._params.annotations_result
        return workflow
