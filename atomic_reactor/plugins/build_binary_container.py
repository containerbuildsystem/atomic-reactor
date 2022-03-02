"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from pathlib import Path

from atomic_reactor.plugin import BuildStepPlugin
from atomic_reactor.utils import retries



class BinaryContainerPlugin(BuildStepPlugin):

    key = 'binary_container'

    def run(self):
        if self.workflow.platform == 'x86_64':
            self.log.info(f'Building image for platform {self.workflow.platform}')
            image_id = self.workflow.data.tag_conf.get_unique_images_with_platform(self.workflow.platform)[0]
            self.log.info(f'image id: {image_id}')

            # TODO, get platform specific path
            dockerfile_path = self.workflow.build_dir.dockerfile_path

            self.log.info('Building image...')
            build_cmd = ['podman', 'build', '-t', image_id, dockerfile_path]

            try:
                retries.run_cmd(build_cmd)
            except subprocess.CalledProcessError as e:
                logger.error("podman build failed\n%s", e.output)
                raise
        else:
            raise RuntimeError(f'platform {self.workflow.platform} not enabled')
