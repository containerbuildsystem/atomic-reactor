"""
Copyright (c) 2017, 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import functools
import hashlib
from collections import namedtuple
from pathlib import Path
from typing import Iterator, Sequence

import koji

from atomic_reactor import util
from atomic_reactor.constants import (PLUGIN_FETCH_MAVEN_KEY,
                                      REPO_FETCH_ARTIFACTS_URL,
                                      REPO_FETCH_ARTIFACTS_KOJI)
from atomic_reactor.config import get_koji_session
from atomic_reactor.dirs import BuildDir
from atomic_reactor.download import download_url
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.utils.koji import NvrRequest
from atomic_reactor.utils.pnc import PNCUtil

try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse

DownloadRequest = namedtuple('DownloadRequest', 'url dest checksums')


class FetchMavenArtifactsPlugin(PreBuildPlugin):
    key = PLUGIN_FETCH_MAVEN_KEY
    is_allowed_to_fail = False

    DOWNLOAD_DIR = 'artifacts'

    def __init__(self, workflow):
        """
        :param workflow: DockerBuildWorkflow instance
        """
        super(FetchMavenArtifactsPlugin, self).__init__(workflow)

        self.path_info = self.workflow.conf.koji_path_info

        all_allowed_domains = self.workflow.conf.artifacts_allowed_domains
        self.allowed_domains = set(domain.lower() for domain in all_allowed_domains or [])
        self.session = None
        self._pnc_util = None

    @property
    def pnc_util(self):
        if not self._pnc_util:
            pnc_map = self.workflow.conf.pnc
            if not pnc_map:
                raise RuntimeError('No PNC configuration found in reactor config map')
            self._pnc_util = PNCUtil(pnc_map)
        return self._pnc_util

    def get_pnc_artifact_ids(self, pnc_requests):
        artifact_ids = []
        builds = pnc_requests.get('builds', [])

        for build in builds:
            for artifact in build['artifacts']:
                artifact_ids.append(artifact['id'])

        return artifact_ids

    def process_by_nvr(self, nvr_requests):
        download_queue = []
        errors = []

        for nvr_request in nvr_requests:
            build_info = self.session.getBuild(nvr_request.nvr)
            if not build_info:
                errors.append('Build {} not found.'.format(nvr_request.nvr))
                continue

            maven_build_path = self.path_info.mavenbuild(build_info)
            build_archives = self.session.listArchives(buildID=build_info['id'],
                                                       type='maven')
            build_archives = nvr_request.match_all(build_archives)

            for build_archive in build_archives:
                maven_file_path = self.path_info.mavenfile(build_archive)
                # NOTE: Don't use urljoin here because maven_build_path does
                # not contain a trailing slash, which causes the last dir to
                # be dropped.
                url = maven_build_path + '/' + maven_file_path
                checksum_type = koji.CHECKSUM_TYPES[build_archive['checksum_type']]
                checksums = {checksum_type: build_archive['checksum']}
                download_queue.append(DownloadRequest(url, maven_file_path, checksums))

            unmatched_archive_requests = nvr_request.unmatched()
            if unmatched_archive_requests:
                errors.append('NVR request for "{}", failed to find archives for: "{}"'
                              .format(nvr_request.nvr, unmatched_archive_requests))
                continue

        if errors:
            raise ValueError('Errors found while processing {}: {}'
                             .format(REPO_FETCH_ARTIFACTS_KOJI, ', '.join(errors)))
        return download_queue

    def process_by_url(self, url_requests):
        download_queue = []
        errors = []

        for url_request in url_requests:
            url = url_request['url']

            if self.allowed_domains:
                parsed_file_url = urlparse(url.lower())
                file_url = parsed_file_url.netloc + parsed_file_url.path
                if not any(file_url.startswith(prefix) for prefix in self.allowed_domains):
                    errors.append('File URL {} is not in list of allowed domains: {}'
                                  .format(file_url, self.allowed_domains))
                    continue

            checksums = {algo: url_request[algo] for algo in hashlib.algorithms_guaranteed
                         if algo in url_request}

            target = url_request.get('target', url.rsplit('/', 1)[-1])
            download_queue.append(DownloadRequest(url, target, checksums))

        if errors:
            raise ValueError('Errors found while processing {}: {}'
                             .format(REPO_FETCH_ARTIFACTS_URL, ', '.join(errors)))

        return download_queue

    def process_pnc_requests(self, pnc_requests):
        download_queue = []
        builds = pnc_requests.get('builds', [])

        for build in builds:
            for artifact in build['artifacts']:
                url, checksums = self.pnc_util.get_artifact(artifact['id'])
                download_queue.append(DownloadRequest(url, artifact['target'], checksums))

        return download_queue

    def download_files(
        self, downloads: Sequence[DownloadRequest], build_dir: BuildDir
    ) -> Iterator[Path]:
        """Download maven artifacts to a build dir."""
        artifacts_path = build_dir.path / self.DOWNLOAD_DIR
        koji_config = self.workflow.conf.koji
        insecure = koji_config.get('insecure_download', False)

        self.log.debug('%d files to download', len(downloads))
        session = util.get_retrying_requests_session()

        for index, download in enumerate(downloads):
            dest_path = artifacts_path / download.dest
            dest_dir = dest_path.parent
            dest_filename = dest_path.name

            if not dest_dir.exists():
                dest_dir.mkdir(parents=True)

            self.log.debug('%d/%d downloading %s', index + 1, len(downloads),
                           download.url)

            download_url(url=download.url, dest_dir=dest_dir, insecure=insecure, session=session,
                         dest_filename=dest_filename, expected_checksums=download.checksums)
            yield dest_path

    def run(self):
        self.session = get_koji_session(self.workflow.conf)

        nvr_requests = [
            NvrRequest(**nvr_request) for nvr_request in
            util.read_fetch_artifacts_koji(self.workflow) or []
        ]
        pnc_requests = util.read_fetch_artifacts_pnc(self.workflow) or {}
        url_requests = util.read_fetch_artifacts_url(self.workflow) or []

        download_queue = (self.process_by_nvr(nvr_requests) +
                          self.process_pnc_requests(pnc_requests) +
                          self.process_by_url(url_requests))

        download_to_build_dir = functools.partial(self.download_files, download_queue)
        self.workflow.build_dir.for_all_platforms_copy(download_to_build_dir)

        pnc_artifact_ids = self.get_pnc_artifact_ids(pnc_requests)

        return {'download_queue': download_queue,
                'pnc_artifact_ids': pnc_artifact_ids}
