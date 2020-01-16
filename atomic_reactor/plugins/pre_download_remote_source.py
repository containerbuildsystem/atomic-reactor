"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Downloads and unpacks the source code archive from Cachito and sets appropriate build args.
"""

from __future__ import absolute_import

import os
import tarfile

from atomic_reactor.constants import REMOTE_SOURCE_DIR
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.download import download_url


class DownloadRemoteSourcePlugin(PreBuildPlugin):
    key = 'download_remote_source'
    is_allowed_to_fail = False
    REMOTE_SOURCE = 'unpacked_remote_sources'

    def __init__(self, tasker, workflow, remote_source_url=None,
                 remote_source_build_args=None):
        """
        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param remote_source_url: URL to download source archive from
        :param remote_source_build_args: dict of container build args
                                         to be used when building the image
        """
        super(DownloadRemoteSourcePlugin, self).__init__(tasker, workflow)
        self.url = remote_source_url
        self.buildargs = remote_source_build_args or {}

    def run(self):
        """
        Run the plugin.
        """
        if not self.url:
            self.log.info('No remote source url to download, skipping plugin')
            return

        # Download the source code archive
        archive = download_url(self.url, self.workflow.source.workdir)

        # Unpack the source code archive into a dedicated dir in container build workdir
        dest_dir = os.path.join(self.workflow.builder.df_dir, self.REMOTE_SOURCE)
        if not os.path.exists(dest_dir):
            os.makedirs(dest_dir)
        else:
            raise RuntimeError('Conflicting path {} already exists in the dist-git repository'
                               .format(self.REMOTE_SOURCE))

        with tarfile.open(archive) as tf:
            tf.extractall(dest_dir)

        # Set build args
        self.workflow.builder.buildargs.update(self.buildargs)

        # To copy the sources into the build image, Dockerfile should contain
        # COPY $REMOTE_SOURCE $REMOTE_SOURCE_DIR
        args_for_dockerfile_to_add = {
            'REMOTE_SOURCE': self.REMOTE_SOURCE,
            'REMOTE_SOURCE_DIR': REMOTE_SOURCE_DIR,
            }
        self.workflow.builder.buildargs.update(args_for_dockerfile_to_add)

        return archive
