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
from shlex import quote

from atomic_reactor.constants import REMOTE_SOURCE_DIR, CACHITO_ENV_FILENAME
from atomic_reactor.download import download_url
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_reactor_config import get_cachito
from atomic_reactor.util import get_retrying_requests_session
from atomic_reactor.utils.cachito import CFG_TYPE_B64


class DownloadRemoteSourcePlugin(PreBuildPlugin):
    key = 'download_remote_source'
    is_allowed_to_fail = False
    REMOTE_SOURCE = 'unpacked_remote_sources'

    def __init__(self, tasker, workflow, remote_sources=None):
        """
        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param remote_sources: list of dicts, each dict contains info about particular
        remote source with the following keys:
            build_args: dict, extra args for `builder.build_args`, if any
            configs: list of str, configuration files to be injected into
            the exploded remote sources dir
            request_id: int, cachito request id; used to request the
            Image Content Manifest
            url: str, URL from which to download a source archive
            name: str, name of remote source
        """
        super(DownloadRemoteSourcePlugin, self).__init__(tasker, workflow)
        self.remote_sources = remote_sources

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

    def generate_cachito_env_file(self):
        """
        Generate cachito.env file with exported environment variables received from
        cachito request.
        """

        self.log.info('Creating %s file with environment variables '
                      'received from cachito request', CACHITO_ENV_FILENAME)

        # Use dedicated dir in container build workdir for cachito.env
        abs_path = os.path.join(self.workflow.builder.df_dir,
                                self.REMOTE_SOURCE, CACHITO_ENV_FILENAME)
        with open(abs_path, 'w') as f:
            f.write('#!/bin/bash\n')
            for env_var, value in self.remote_sources[0]['build_args'].items():
                f.write('export {}={}\n'.format(env_var, quote(value)))

    def run(self):
        """
        Run the plugin.
        """
        if not self.remote_sources:
            self.log.info('Missing remote_sources parameters, skipping plugin')
            return

        session = get_retrying_requests_session()

        # Download the source code archive
        cachito_config = get_cachito(self.workflow)
        insecure_ssl_conn = cachito_config.get('insecure', False)
        archive = download_url(
            self.remote_sources[0]['url'],
            self.workflow.source.workdir,
            session=session,
            insecure=insecure_ssl_conn
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
            self.get_remote_source_config(
                session, self.remote_sources[0]["configs"], insecure_ssl_conn
            )
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
        self.workflow.builder.buildargs.update(self.remote_sources[0]['build_args'])

        # Create cachito.env file with environment variables received from cachito request
        self.generate_cachito_env_file()

        # To copy the sources into the build image, Dockerfile should contain
        # COPY $REMOTE_SOURCE $REMOTE_SOURCE_DIR
        args_for_dockerfile_to_add = {
            'REMOTE_SOURCE': self.REMOTE_SOURCE,
            'REMOTE_SOURCE_DIR': REMOTE_SOURCE_DIR,
            }
        self.workflow.builder.buildargs.update(args_for_dockerfile_to_add)

        return archive
