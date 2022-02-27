"""
Copyright (c) 2015-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import os

from atomic_reactor.constants import (EXPORTED_COMPRESSED_IMAGE_NAME_TEMPLATE,
                                      IMAGE_TYPE_DOCKER_ARCHIVE)
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.util import get_exported_image_metadata, is_scratch_build


class FetchDockerArchivePlugin(PostBuildPlugin):
    key = 'fetch_docker_archive'
    is_allowed_to_fail = False

    def __init__(self, workflow):
        """
        :param workflow: DockerBuildWorkflow instance
        """
        super(FetchDockerArchivePlugin, self).__init__(workflow)
        self.source_build = bool(self.workflow.data.build_result.source_docker_archive)

    def run(self):
        if is_scratch_build(self.workflow):
            # required only to make an archive for Koji
            self.log.info('scratch build, skipping plugin')
            return

        if self.source_build:
            self.log.info('skipping, no exported source image')
            return
        image = self.workflow.image
        image_type = IMAGE_TYPE_DOCKER_ARCHIVE
        self.log.info('fetching image %s from docker', image)
        # OSBS2 TBD
        # Set image_tarball_path, if image tarball is not yet
        # stored somewhere, download it
        # with imageutil.download_image_archive_tarball() for each platform
        outfile = os.path.join(self.workflow.source.workdir,
                               EXPORTED_COMPRESSED_IMAGE_NAME_TEMPLATE)
        os.mknod(outfile)
        metadata = get_exported_image_metadata(outfile, image_type)

        self.workflow.data.exported_image_sequence.append(metadata)
        self.log.info('image is available as %s', outfile)
