"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import hashlib
import os
import re
from collections import namedtuple

import koji

from atomic_reactor import util
from atomic_reactor.constants import (KOJI_BTYPE_REMOTE_SOURCE_FILE,
                                      PLUGIN_GENERATE_MAVEN_METADATA_KEY)
from atomic_reactor.download import download_url
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.plugins.pre_reactor_config import get_koji
from atomic_reactor.plugins.pre_reactor_config import get_koji_session
from atomic_reactor.utils.koji import NvrRequest

DownloadRequest = namedtuple('DownloadRequest', 'url dest checksums')


class GenerateMavenMetadataPlugin(PostBuildPlugin):
    """
    Generate maven metadata
    """

    key = PLUGIN_GENERATE_MAVEN_METADATA_KEY
    is_allowed_to_fail = False
    DOWNLOAD_DIR = 'url_sources'

    def __init__(self, tasker, workflow):
        """
        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        """
        super(GenerateMavenMetadataPlugin, self).__init__(tasker, workflow)
        self.session = None
        self.workdir = self.workflow.source.get_build_file_path()[1]
        self.no_source_artifacts = []
        self.source_url_to_artifacts = {}

    def get_nvr_components(self, nvr_requests):
        components = []

        for nvr_request in nvr_requests:
            # We're assuming here that we won't run into any errors here
            #  since this plugin runs after pre_fetch_maven_artifacts
            #  that should fail if there were any errors.
            build_info = self.session.getBuild(nvr_request.nvr)
            build_archives = self.session.listArchives(buildID=build_info['id'],
                                                       type='maven')
            build_archives = nvr_request.match_all(build_archives)

            for build_archive in build_archives:
                checksum_type = koji.CHECKSUM_TYPES[build_archive['checksum_type']]
                components.append({
                    'type': 'kojifile',
                    'filename': build_archive['filename'],
                    'filesize': build_archive['size'],
                    'checksum': build_archive['checksum'],
                    'checksum_type': checksum_type,
                    'nvr': nvr_request.nvr,
                    'archive_id': build_archive['id'],
                })

        return components

    def get_pnc_build_metadata(self, pnc_requests):
        builds = pnc_requests.get('builds', [])

        if not builds:
            return {}

        pnc_build_metadata = {'builds': []}

        for build in builds:
            pnc_build_metadata['builds'].append({'id': build['build_id']})

        return pnc_build_metadata

    def process_url_requests(self, url_requests):
        download_queue = []

        for url_request in url_requests:
            artifact_checksums = {algo: url_request[algo] for algo in
                                  hashlib.algorithms_guaranteed
                                  if algo in url_request}

            artifact = {
                'url': url_request['url'],
                'checksums': artifact_checksums,
                'filename': os.path.basename(url_request['url'])
            }

            if 'source-url' not in url_request:
                self.no_source_artifacts.append(artifact)
                msg = f"No source-url found for {url_request['url']}.\n"
                self.log.warning(msg)
                msg += 'fetch-artifacts-url without source-url is deprecated\n'
                msg += 'to fix this please provide the source-url according to ' \
                       'https://osbs.readthedocs.io/en/latest/users.html#fetch-artifacts-url-yaml'
                self.log.user_warning(msg)
                continue

            source_url = url_request['source-url']

            checksums = {algo: url_request[('source-' + algo)] for algo in
                         hashlib.algorithms_guaranteed
                         if ('source-' + algo) in url_request}

            if source_url not in self.source_url_to_artifacts:
                self.source_url_to_artifacts[source_url] = [artifact]
                # source_url will mostly be gerrit URLs that don't have filename
                #  in the URL itself so we'll have to get filename from URL response
                target = os.path.basename(source_url)

                download_queue.append(DownloadRequest(source_url, target, checksums))
            else:
                self.source_url_to_artifacts[source_url].append(artifact)
        return download_queue

    def download_sources(self, download_queue):
        remote_source_files = []
        downloads_path = os.path.join(self.workdir, self.DOWNLOAD_DIR)

        session = util.get_retrying_requests_session()

        self.log.debug('%d files to download', len(download_queue))

        koji_config = get_koji(self.workflow)
        insecure = koji_config.get('insecure_download', False)

        for index, download in enumerate(download_queue):
            dest_filename = download.dest
            if not re.fullmatch(r'^[\w\-.]+$', dest_filename):
                dest_filename = session.head(download.url).headers.get(
                    "Content-disposition").split("filename=")[1].replace('"', '')

            dest_path = os.path.join(downloads_path, dest_filename)
            dest_dir = os.path.dirname(dest_path)

            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir)

            self.log.debug('%d/%d downloading %s', index + 1, len(download_queue),
                           download.url)

            download_url(url=download.url, dest_dir=dest_dir, insecure=insecure,
                         session=session, dest_filename=dest_filename,
                         expected_checksums=download.checksums)

            checksum_type = list(download.checksums.keys())[0]

            remote_source_files.append({
                'file': dest_path,
                'metadata': {
                    'type': KOJI_BTYPE_REMOTE_SOURCE_FILE,
                    'checksum_type': checksum_type,
                    'checksum': download.checksums[checksum_type],
                    'filename': dest_filename,
                    'filesize': os.path.getsize(dest_path),
                    'extra': {
                        'source-url': download.url,
                        'artifacts': self.source_url_to_artifacts[download.url],
                        'typeinfo': {
                            KOJI_BTYPE_REMOTE_SOURCE_FILE: {}
                        },
                    },
                }})

        return remote_source_files

    def run(self):
        """
        Run the plugin.
        """

        self.session = get_koji_session(self.workflow)

        nvr_requests = [
            NvrRequest(**nvr_request) for nvr_request in
            util.read_fetch_artifacts_koji(self.workflow) or []
        ]
        pnc_requests = util.read_fetch_artifacts_pnc(self.workflow) or {}
        url_requests = util.read_fetch_artifacts_url(self.workflow) or []

        components = self.get_nvr_components(nvr_requests)
        pnc_build_metadata = self.get_pnc_build_metadata(pnc_requests)
        download_queue = self.process_url_requests(url_requests)
        remote_source_files = self.download_sources(download_queue)

        return {'components': components,
                'no_source': self.no_source_artifacts,
                'pnc_build_metadata': pnc_build_metadata,
                'remote_source_files': remote_source_files}
