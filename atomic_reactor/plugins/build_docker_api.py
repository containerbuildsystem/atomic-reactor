"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import print_function, unicode_literals

from atomic_reactor.build import BuildResult
from atomic_reactor.plugin import BuildStepPlugin
from atomic_reactor.util import wait_for_command
from dockerfile_parse import DockerfileParser


class DockerApiPlugin(BuildStepPlugin):
    """
    TODO
    """

    key = 'docker_api'
    is_allowed_to_fail = False

    def run(self):
        """
        build image inside current environment;
        it's expected this may run within (privileged) docker container


        Input:
            df_dir
            image

        Output:
            BuildResult
            built_image_info
            image_id
        """
        builder = self.workflow.builder

        logs_gen = self.tasker.build_image_from_path(
            builder.df_dir,
            builder.image,
        )

        self.log.debug('build is submitted, waiting for it to finish')
        command_result = wait_for_command(logs_gen)

        if command_result.is_failed():
            raise RuntimeError('Image not built!')

        return command_result
