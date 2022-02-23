"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import logging
from pathlib import Path

from atomic_reactor import inner
from atomic_reactor.dirs import ContextDir, RootBuildDir
from atomic_reactor.inner import ImageBuildWorkflowData
from atomic_reactor.tasks import common
from atomic_reactor.tasks import PluginsDef

# PluginsDef can be considered as part of this module, but is defined elsewhere to avoid cyclic
#   imports between the `inner` module and this module
__all__ = ["PluginsDef", "PluginBasedTask"]

logger = logging.getLogger(__name__)


class PluginBasedTask(common.Task):
    """Task that executes a predefined list of plugins."""

    def _get_build_dir(self) -> RootBuildDir:
        """Return the root build directory."""
        return RootBuildDir(Path(self._params.build_dir))

    # Override this in subclasses
    plugins_def: PluginsDef = NotImplemented

    def execute(self):
        """Execute the plugins defined in plugins_def."""
        context_dir = ContextDir(Path(self._params.context_dir))
        wf_data = ImageBuildWorkflowData.load_from_dir(context_dir)

        workflow = inner.DockerBuildWorkflow(
            self._get_build_dir(),
            wf_data,
            source=self._params.source,
            plugins=self.plugins_def,
            user_params=self._params.user_params,
            reactor_config_path=self._params.config_file,
        )

        try:
            try:
                result = workflow.build_docker_image()
            except Exception as e:
                logger.error("task failed: %s", e)
                raise

            if result and result.is_failed():
                msg = f"task failed: {result.fail_reason}"
                logger.error(msg)
                raise RuntimeError(msg)

        finally:
            # For whatever the reason a build fails, always write the workflow
            # data into the data file.
            wf_data.save(context_dir)

        # OSBS2 TBD: OSBS used to log the original Dockerfile after executing the workflow.
        #   It probably doesn't make sense to do that here, but it would be good to log the
        #   Dockerfile somewhere at the end of the build process.
        logger.info(r"task finished successfully \o/")
