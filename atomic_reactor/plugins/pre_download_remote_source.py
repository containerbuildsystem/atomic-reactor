"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Downloads and unpacks the source code archive from Cachito and sets appropriate build args.
"""

from __future__ import absolute_import

import tarfile

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.download import download_url


class DownloadRemoteSourcePlugin(PreBuildPlugin):
    key = 'download_remote_source'
    is_allowed_to_fail = False

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

        # Unpack the source code archive into the workdir
        with tarfile.open(archive) as tf:
            tf.extractall(self.workflow.source.workdir)

        # Set build args
        self.workflow.builder.buildargs.update(self.buildargs)

        return archive
