"""
Copyright (c) 2021-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import logging
from typing import Optional, ClassVar, List, Dict, Any

from atomic_reactor import inner, util
from atomic_reactor.tasks.common import Task, ParamsT
from atomic_reactor.util import get_platforms


__all__ = ["PluginBasedTask"]

logger = logging.getLogger(__name__)


class PluginBasedTask(Task[ParamsT]):
    """Task that executes a predefined list of plugins."""

    # Indicate whether to keep running next plugin if error is raised from
    # previous one. Defaults to False.
    keep_plugins_running: ClassVar[bool] = False

    # Specify the plugin configuration used by PluginsRunner to find out and
    # run the specific plugins. Example:
    #   {"name": "add_filesystem", "args": {...}}
    # Refer to plugins.json schema for the details.
    plugins_conf: ClassVar[List[Dict[str, Any]]] = []
    task_name = 'default'

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
        )
        return workflow

    def execute(self, init_build_dirs: Optional[bool] = False):
        """Execute the plugins defined in plugins_def.

        :param init_build_dirs: bool, whether to initialize build dirs
        :return: None
        """
        util.validate_with_schema(
            {"plugins_conf": self.plugins_conf}, "schemas/plugins.json"
        )

        workflow = self.prepare_workflow()

        if init_build_dirs:
            workflow.build_dir.init_build_dirs(get_platforms(workflow.data), workflow.source)

        try:
            workflow.build_docker_image()
        except Exception as e:
            logger.error("task %s failed: %s", self.task_name, e)
            raise

        # OSBS2 TBD: OSBS used to log the original Dockerfile after executing the workflow.
        #   It probably doesn't make sense to do that here, but it would be good to log the
        #   Dockerfile somewhere at the end of the build process.
        logger.info(r"task %s finished successfully \o/", self.task_name)
