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

from atomic_reactor.constants import REMOTE_SOURCE_DIR, CACHITO_ENV_FILENAME, CACHITO_ENV_ARG_ALIAS
from atomic_reactor.download import download_url
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import get_retrying_requests_session, map_to_user_params
from atomic_reactor.utils.cachito import CFG_TYPE_B64
from urllib.parse import urlparse


class DownloadRemoteSourcePlugin(PreBuildPlugin):
    key = 'download_remote_source'
    is_allowed_to_fail = False
    REMOTE_SOURCE = 'unpacked_remote_sources'

    args_from_user_params = map_to_user_params("remote_sources")

    def __init__(self, workflow, remote_sources=None):
        """
        :param workflow: DockerBuildWorkflow instance
        :param remote_sources: list of dicts, each dict contains info about particular
        remote source with the following keys:
            build_args: dict, extra args for `workflow.buildargs`, if any
            configs: list of str, configuration files to be injected into
            the exploded remote sources dir
            request_id: int, cachito request id; used to request the
            Image Content Manifest
            url: str, URL from which to download a source archive
            name: str, name of remote source
        """
        super(DownloadRemoteSourcePlugin, self).__init__(workflow)
        self.remote_sources = remote_sources
        self.multiple_remote_sources = bool(self.workflow.source.config.remote_sources)

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

    def generate_cachito_env_file(self, dest_dir, build_args):
        """
        Generate cachito.env file with exported environment variables received from
        cachito request.

        :param dest_dir: str, destination directory for env file
        :param build_args: dict, build arguments to set
        """
        self.log.info('Creating %s file with environment variables '
                      'received from cachito request', CACHITO_ENV_FILENAME)

        # Use dedicated dir in container build workdir for cachito.env
        abs_path = os.path.join(dest_dir, CACHITO_ENV_FILENAME)
        with open(abs_path, 'w') as f:
            f.write('#!/bin/bash\n')
            for env_var, value in build_args.items():
                f.write('export {}={}\n'.format(env_var, quote(value)))

    def generate_cachito_config_files(self, dest_dir, config_files):
        """Inject cachito provided configuration files

        :param dest_dir: str, destination directory for config files
        :param config_files: list[dict], configuration files from cachito
        """
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

    def add_general_buildargs(self):
        """Adds general build arguments

        To copy the sources into the build image, Dockerfile should contain
        COPY $REMOTE_SOURCE $REMOTE_SOURCE_DIR
        or COPY $REMOTE_SOURCES $REMOTE_SOURCES_DIR
        """
        if self.multiple_remote_sources:
            args_for_dockerfile_to_add = {
                'REMOTE_SOURCES': self.REMOTE_SOURCE,
                'REMOTE_SOURCES_DIR': REMOTE_SOURCE_DIR,
                }
        else:
            args_for_dockerfile_to_add = {
                'REMOTE_SOURCE': self.REMOTE_SOURCE,
                'REMOTE_SOURCE_DIR': REMOTE_SOURCE_DIR,
                CACHITO_ENV_ARG_ALIAS: os.path.join(REMOTE_SOURCE_DIR, CACHITO_ENV_FILENAME),
                }
        self.workflow.buildargs.update(args_for_dockerfile_to_add)

    def run(self):
        """
        Run the plugin.
        """
        if not self.remote_sources:
            self.log.info('Missing remote_sources parameters, skipping plugin')
            return

        session = get_retrying_requests_session()

        archives = []
        cachito_config = self.workflow.conf.cachito
        insecure_ssl_conn = cachito_config.get('insecure', False)

        for remote_source in self.remote_sources:
            parsed_url = urlparse(remote_source['url'])
            dest_filename = os.path.basename(parsed_url.path)
            # prepend remote source name to destination filename, so multiple source archives
            # don't have name collision
            if self.multiple_remote_sources:
                dest_filename = "{}_{}".format(remote_source['name'], dest_filename)

            # Download the source code archive
            archive = download_url(
                remote_source['url'],
                self.workflow.source.workdir,
                session=session,
                insecure=insecure_ssl_conn,
                dest_filename=dest_filename
            )
            archives.append(archive)

            # Unpack the source code archive into a dedicated dir in container build workdir
            dest_dir = os.path.join(self.workflow.df_dir, self.REMOTE_SOURCE)
            sub_path = self.REMOTE_SOURCE

            if self.multiple_remote_sources:
                dest_dir = os.path.join(dest_dir, remote_source['name'])
                sub_path = os.path.join(sub_path, remote_source['name'])

            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir)
            else:
                raise RuntimeError('Conflicting path {} already exists in the dist-git repository'
                                   .format(sub_path))

            with tarfile.open(archive) as tf:
                tf.extractall(dest_dir)

            config_files = (
                self.get_remote_source_config(
                    session, remote_source["configs"], insecure_ssl_conn
                )
            )

            self.generate_cachito_config_files(dest_dir, config_files)

            # Set build args
            if not self.multiple_remote_sources:
                self.workflow.buildargs.update(remote_source['build_args'])

            # Create cachito.env file with environment variables received from cachito request
            self.generate_cachito_env_file(dest_dir, remote_source['build_args'])

        self.add_general_buildargs()

        return archives
