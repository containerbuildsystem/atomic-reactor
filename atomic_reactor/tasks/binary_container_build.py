"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import logging
import subprocess
from dataclasses import dataclass
from typing import Dict, Iterator

from osbs.utils import ImageName

from atomic_reactor import dirs
from atomic_reactor import util
from atomic_reactor.tasks.common import Task, TaskParams
from atomic_reactor.utils import retries


logger = logging.getLogger(__name__)


class BuildTaskError(Exception):
    """The build task failed."""


class BuildProcessError(BuildTaskError):
    """The subprocess that was supposed to build the image failed."""


class PushError(BuildTaskError):
    """Failed to push the built image."""


@dataclass(frozen=True)
class BinaryBuildTaskParams(TaskParams):
    """Binary container build task parameters"""
    platform: str


class BinaryBuildTask(Task):
    """Binary container build task."""

    def execute(self):
        """Build a container image for the platform specified in the task parameters.

        The built image will be pushed to the unique tag for this platform, which can be found
        in tag_conf.get_unique_images_with_platform() (where tag_conf comes from context data).
        """
        platform = self._params.platform

        data = self.load_workflow_data()
        enabled_platforms = util.get_platforms(data)

        if platform not in enabled_platforms:
            logger.info(
                r"Platform %s is not enabled for this build (enabled platforms: %s). Exiting.",
                platform,
                enabled_platforms,
            )
            return

        build_dir = self.get_build_dir().platform_dir(platform)
        dest_tag = data.tag_conf.get_unique_images_with_platform(platform)[0]

        logger.info("Building for the %s platform from %s", platform, build_dir.dockerfile_path)

        try:
            output_lines = self.build_container(
                build_dir=build_dir,
                build_args=data.buildargs,
                dest_tag=dest_tag,
            )
            for line in output_lines:
                logger.info(line.rstrip())

            logger.info("Build finished succesfully! Pushing image to %s", dest_tag)
            self.push_container(dest_tag)
        finally:
            logger.info("Dockerfile used for build:\n%s", build_dir.dockerfile_path.read_text())

    def build_container(
        self,
        *,
        build_dir: dirs.BuildDir,
        build_args: Dict[str, str],
        dest_tag: ImageName,
    ) -> Iterator[str]:
        """Build a container image from the specified build directory.

        Pass the specified build arguments as ARG values using --build-arg.

        The built image will be available locally on the machine that built it, tagged with
        the specified dest_tag.

        This method returns an iterator which yields individual lines from the stdout
        and stderr of the build process as they become available.
        """
        build_cmd = ["/bin/sh", "-c", "for i in 1 2 3; do echo output $i; sleep 0.1; done"]
        logger.debug("Running %s", " ".join(build_cmd))

        build_process = subprocess.Popen(
            build_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
        )

        # passing stdout=PIPE guarantees that stdout is not None, but the type hints for the
        #   subprocess module do not express that (TL;DR - this is just for type checkers)
        assert build_process.stdout is not None

        last_line = None

        while True:
            if line := build_process.stdout.readline():
                yield line
                last_line = line

            if (rc := build_process.poll()) is not None:
                break

        if rc != 0:
            # the last line of output likely contains the error message
            error = last_line.rstrip() if last_line else "<no output!>"
            raise BuildProcessError(f"Build failed (rc={rc}): {error}")

    def push_container(self, dest_tag: ImageName) -> None:
        """Push the built container (named dest_tag) to the registry (as dest_tag)."""
        push_cmd = ["echo", str(dest_tag)]
        try:
            retries.run_cmd(push_cmd)
        except subprocess.CalledProcessError as e:
            raise PushError(
                f"Push failed (rc={e.returncode}). Check the logs for more details."
            ) from e
