"""
Copyright (c) 2021-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os
import re
from typing import Any, Dict, List, Sequence

from atomic_reactor import util
from atomic_reactor.constants import (KOJI_BTYPE_REMOTE_SOURCE_FILE, PLUGIN_FETCH_MAVEN_KEY,
                                      PLUGIN_MAVEN_URL_SOURCES_METADATA_KEY)
from atomic_reactor.download import download_url
from atomic_reactor.plugin import Plugin
from atomic_reactor.plugins.fetch_maven_artifacts import DownloadRequest


class MavenURLSourcesMetadataPlugin(Plugin):
    """
    Generate maven metadata
    """

    key = PLUGIN_MAVEN_URL_SOURCES_METADATA_KEY
    is_allowed_to_fail = False
    DOWNLOAD_DIR = 'url_sources'

    def __init__(self, workflow):
        """
        :param workflow: DockerBuildWorkflow instance
        """
        super(MavenURLSourcesMetadataPlugin, self).__init__(workflow)
        self.source_url_to_artifacts = {}

    def get_remote_source_files(
            self, download_queue: Sequence[DownloadRequest]
    ) -> List[Dict[str, Any]]:
        remote_source_files = []
        downloads_path = self.workflow.build_dir.any_platform.path / self.DOWNLOAD_DIR

        session = util.get_retrying_requests_session()

        self.log.debug('%d url source files to download', len(download_queue))

        koji_config = self.workflow.conf.koji
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
        fetch_maven_result = self.workflow.data.plugins_results[PLUGIN_FETCH_MAVEN_KEY]
        source_download_queue = [DownloadRequest(**x) for x in fetch_maven_result.get(
            'source_download_queue')]
        self.source_url_to_artifacts = fetch_maven_result['source_url_to_artifacts']
        remote_source_files = self.get_remote_source_files(source_download_queue)

        return {'remote_source_files': remote_source_files}
