"""
Copyright (c) 2021-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import logging
from typing import Optional

from atomic_reactor import inner
from atomic_reactor.tasks import PluginsDef
from atomic_reactor.tasks.common import Task, ParamsT
from atomic_reactor.util import get_platforms


# PluginsDef can be considered as part of this module, but is defined elsewhere to avoid cyclic
#   imports between the `inner` module and this module
__all__ = ["PluginsDef", "PluginBasedTask"]

logger = logging.getLogger(__name__)


class PluginBasedTask(Task[ParamsT]):
    """Task that executes a predefined list of plugins."""

    # Override this in subclasses
    plugins_def: PluginsDef = NotImplemented

    def prepare_workflow(self) -> inner.DockerBuildWorkflow:
        """Fully initialize the workflow instance to be used for running the list of plugins."""
        workflow = inner.DockerBuildWorkflow(
            build_dir=self.get_build_dir(),
            data=self.load_workflow_data(),
            namespace=self._params.namespace,
            pipeline_run_name=self._params.pipeline_run_name,
            source=self._params.source,
            plugins=self.plugins_def,
            user_params=self._params.user_params,
            reactor_config_path=self._params.config_file,
        )
        return workflow

    def execute(self, init_build_dirs: Optional[bool] = False):
        """Execute the plugins defined in plugins_def.

        :param init_build_dirs: bool, whether to initialize build dirs
        :return: None
        """
        workflow = self.prepare_workflow()

        if init_build_dirs:
            workflow.build_dir.init_build_dirs(get_platforms(workflow.data), workflow.source)

        try:
            workflow.build_docker_image()
        except Exception as e:
            logger.error("task failed: %s", e)
            raise
        finally:
            # For whatever the reason a build fails, always write the workflow
            # data into the data file.
            workflow.data.save(self.get_context_dir())

        # OSBS2 TBD: OSBS used to log the original Dockerfile after executing the workflow.
        #   It probably doesn't make sense to do that here, but it would be good to log the
        #   Dockerfile somewhere at the end of the build process.
        logger.info(r"task finished successfully \o/")
