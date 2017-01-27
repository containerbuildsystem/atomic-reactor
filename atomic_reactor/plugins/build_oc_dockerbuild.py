"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import print_function, unicode_literals

from atomic_reactor.build import BuildResult
from atomic_reactor.constants import EXPORTED_BUILT_IMAGE_NAME
from atomic_reactor.plugin import BuildStepPlugin, InappropriateBuildStepError
from atomic_reactor.util import get_exported_image_metadata, wait_for_command
from dockerfile_parse import DockerfileParser

from subprocess import Popen, PIPE, STDOUT
import os.path


class OCDockerbuildPlugin(BuildStepPlugin):

    key = 'oc_dockerbuild'
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, export_image=False):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param export_image: bool, when True, built image is saved to archive
        """
        super(OCDockerbuildPlugin, self).__init__(tasker, workflow)
        self.export_image = export_image

    def run(self):
        builder = self.workflow.builder

        raise InappropriateBuildStepError('Nah!')

        image = builder.image.to_str()
        oc_process = Popen([
            'oc',
            'ex',
            'dockerbuild',
            builder.df_dir,
            image,
        ], stdout=PIPE, stderr=STDOUT)

        self.log.debug('build is submitted, waiting for it to finish')
        lines = []
        with oc_process.stdout:
            for line in iter(oc_process.stdout.readline, ''):
                self.log.info(line.strip())
                lines.append(line)
        oc_process.wait()

        # TODO: Error detection is not working!
        command_result = wait_for_command(line for line in lines)

        if oc_process.returncode != 0:
            raise RuntimeError('Image not built!')

        if self.export_image:
            self.log.info('saving image into archive')
            outfile = os.path.join(self.workflow.source.workdir,
                                   EXPORTED_BUILT_IMAGE_NAME)

            with open(outfile, 'w+b') as archive:
                archive.write(self.tasker.d.get_image(image).data)

            metadata = get_exported_image_metadata(outfile)
            self.workflow.exported_image_sequence.append(metadata)

        return command_result
