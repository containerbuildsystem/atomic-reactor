"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Downloads and unpacks the source code archive from Cachito and sets appropriate build args.
"""

import base64
import os
import tarfile

from atomic_reactor.constants import REMOTE_SOURCE_DIR
from atomic_reactor.download import download_url
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_reactor_config import get_cachito
from atomic_reactor.util import get_retrying_requests_session
from atomic_reactor.utils.cachito import CFG_TYPE_B64


class DownloadRemoteSourcePlugin(PreBuildPlugin):
    key = 'download_remote_source'
    is_allowed_to_fail = False
    REMOTE_SOURCE = 'unpacked_remote_sources'

    def __init__(self, tasker, workflow, remote_source_url=None,
                 remote_source_build_args=None,
                 remote_source_configs=None):
        """
        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param remote_source_url: URL to download source archive from
        :param remote_source_build_args: dict of container build args
                                         to be used when building the image
        :param remote_source_configs: URL to fetch a list with configuration files data to be
                                      injected in the exploded remote sources dir
        """
        super(DownloadRemoteSourcePlugin, self).__init__(tasker, workflow)
        self.url = remote_source_url
        self.buildargs = remote_source_build_args or {}
        self.remote_source_conf_url = remote_source_configs

    def get_remote_source_config(self, session, url, insecure=False):
        """Get the configuration files associated with the remote sources

        :param session: the requests HTTP session object.
        :param url: str, URL to cachito remote source configurations
        :param insecure: bool, whether to verify SSL certificates
        :return: list[dict], configuration data for the given request.
                 Entries include path, type, and content.
        """
        self.log.info('Checking for additional configurations at %s', url)
        response = session.get(url, verify=not insecure)
        response.raise_for_status()
        return response.json()

    def run(self):
        """
        Run the plugin.
        """
        if not self.url:
            self.log.info('No remote source url to download, skipping plugin')
            return

        session = get_retrying_requests_session()

        # Download the source code archive
        cachito_config = get_cachito(self.workflow)
        insecure_ssl_conn = cachito_config.get('insecure', False)
        archive = download_url(
            self.url, self.workflow.source.workdir, session=session, insecure=insecure_ssl_conn
        )

        # Unpack the source code archive into a dedicated dir in container build workdir
        dest_dir = os.path.join(self.workflow.builder.df_dir, self.REMOTE_SOURCE)
        if not os.path.exists(dest_dir):
            os.makedirs(dest_dir)
        else:
            raise RuntimeError('Conflicting path {} already exists in the dist-git repository'
                               .format(self.REMOTE_SOURCE))

        with tarfile.open(archive) as tf:
            tf.extractall(dest_dir)

        config_files = (
            self.get_remote_source_config(session, self.remote_source_conf_url, insecure_ssl_conn)
            if self.remote_source_conf_url else []
        )

        # Inject cachito provided configuration files
        for config in config_files:
            config_path = os.path.join(dest_dir, config['path'])
            if config['type'] == CFG_TYPE_B64:
                data = base64.b64decode(config['content'])
                with open(config_path, 'wb') as f:
                    f.write(data)
            else:
                err_msg = "Unknown cachito configuration file data type '{}'".format(config['type'])
                raise ValueError(err_msg)

            os.chmod(config_path, 0o444)

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
