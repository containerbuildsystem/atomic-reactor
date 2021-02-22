"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import fnmatch
import hashlib
import koji
import os

from atomic_reactor import util
from atomic_reactor.constants import (DEFAULT_DOWNLOAD_BLOCK_SIZE,
                                      PLUGIN_FETCH_MAVEN_KEY,
                                      REPO_FETCH_ARTIFACTS_URL,
                                      REPO_FETCH_ARTIFACTS_KOJI)
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_reactor_config import (get_koji_session,
                                                       get_koji_path_info,
                                                       get_artifacts_allowed_domains)
from collections import namedtuple

try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse


class NvrRequest(object):

    def __init__(self, nvr, archives=None):
        self.nvr = nvr
        self.archives = archives or []

        for archive in self.archives:
            archive['matched'] = False

    def match(self, build_archive):
        if not self.archives:
            return True

        for archive in self.archives:
            req_filename = archive.get('filename')
            req_group_id = archive.get('group_id')

            if req_filename and not fnmatch.filter([build_archive['filename']],
                                                   req_filename):
                continue

            if req_group_id and req_group_id != build_archive['group_id']:
                continue

            archive['matched'] = True
            return True

        return False

    def match_all(self, build_archives):
        return [archive for archive in build_archives if self.match(archive)]

    def unmatched(self):
        return [archive for archive in self.archives if not archive['matched']]


DownloadRequest = namedtuple('DownloadRequest', 'url dest checksums')


class PNCRequest(object):
    # TODO implement logic to interact with PNC and process artifacts

    def __init__(self, build_id, artifacts=None):
        self.build_id = build_id
        self.artifacts = artifacts or []


class FetchMavenArtifactsPlugin(PreBuildPlugin):

    key = PLUGIN_FETCH_MAVEN_KEY
    is_allowed_to_fail = False

    DOWNLOAD_DIR = 'artifacts'

    def __init__(self, tasker, workflow, allowed_domains=None):
        """
        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param allowed_domains: list<str>: list of domains that are
               allowed to be used when fetching artifacts by URL (case insensitive)
        """
        super(FetchMavenArtifactsPlugin, self).__init__(tasker, workflow)

        self.path_info = get_koji_path_info(self.workflow)

        all_allowed_domains = get_artifacts_allowed_domains(self.workflow, allowed_domains or [])
        self.allowed_domains = set(domain.lower() for domain in all_allowed_domains or [])
        self.workdir = self.workflow.source.get_build_file_path()[1]
        self.session = None

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

    def process_by_pnc_build_id(self, pnc_requests):
        # TODO use PNC request to process all the build ids
        pass

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

    def download_files(self, downloads):
        artifacts_path = os.path.join(self.workdir, self.DOWNLOAD_DIR)

        self.log.debug('%d files to download', len(downloads))
        session = util.get_retrying_requests_session()

        for index, download in enumerate(downloads):
            dest_path = os.path.join(artifacts_path, download.dest)
            dest_dir = dest_path.rsplit('/', 1)[0]
            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir)

            self.log.debug('%d/%d downloading %s', index + 1, len(downloads),
                           download.url)

            checksums = {algo: hashlib.new(algo) for algo in download.checksums}
            request = session.get(download.url, stream=True)
            request.raise_for_status()

            with open(dest_path, 'wb') as f:
                for chunk in request.iter_content(chunk_size=DEFAULT_DOWNLOAD_BLOCK_SIZE):
                    f.write(chunk)
                    for checksum in checksums.values():
                        checksum.update(chunk)

            for algo, checksum in checksums.items():
                if checksum.hexdigest() != download.checksums[algo]:
                    raise ValueError(
                        'Computed {} checksum, {}, does not match expected checksum, {}'
                        .format(algo, checksum.hexdigest(), download.checksums[algo]))

    def run(self):
        self.session = get_koji_session(self.workflow)

        nvr_requests = [
            NvrRequest(**nvr_request) for nvr_request in
            util.read_fetch_artifacts_koji(self.workflow) or []
        ]
        pnc_requests = [
            PNCRequest(**pnc_request) for pnc_request in
            (util.read_fetch_artifacts_pnc(self.workflow) or {'builds': []})['builds']
        ]
        url_requests = util.read_fetch_artifacts_url(self.workflow) or []

        download_queue = (self.process_by_nvr(nvr_requests) +
                          self.process_by_url(url_requests) +
                          (self.process_by_pnc_build_id(pnc_requests) or []))

        self.download_files(download_queue)

        # TODO: Return a list of files for koji metadata
        return download_queue
