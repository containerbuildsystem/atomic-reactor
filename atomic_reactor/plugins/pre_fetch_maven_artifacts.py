"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals

import fnmatch
import hashlib
import koji
import os
import requests

from atomic_reactor import util
from atomic_reactor.koji_util import create_koji_session
from atomic_reactor.plugin import PreBuildPlugin
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
        return filter(self.match, build_archives)

    def unmatched(self):
        return [archive for archive in self.archives if not archive['matched']]


DownloadRequest = namedtuple('DownloadRequest', 'url dest checksums')


class FetchMavenArtifactsPlugin(PreBuildPlugin):

    key = 'fetch_maven_artifacts'
    is_allowed_to_fail = False

    NVR_REQUESTS_FILENAME = 'fetch-artifacts-koji.yaml'
    URL_REQUESTS_FILENAME = 'fetch-artifacts-url.yaml'

    DOWNLOAD_DIR = 'artifacts'

    def __init__(self, tasker, workflow, koji_hub, koji_root,
                 koji_proxyuser=None, koji_ssl_certs_dir=None,
                 koji_krb_principal=None, koji_krb_keytab=None,
                 allowed_domains=None):
        """
        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param koji_hub: str, koji hub (xmlrpc)
        :param koji_root: str, koji root (storage)
        :param koji_proxyuser: str, proxy user
        :param koji_ssl_certs_dir: str, path to "cert", "ca", and "serverca"
        :param koji_krb_principal: str, name of Kerberos principal
        :param koji_krb_keytab: str, Kerberos keytab
        :param allowed_domains: list<str>: list of domains that are
               allowed to be used when fetching artifacts by URL (case insensitive)
        """
        super(FetchMavenArtifactsPlugin, self).__init__(tasker, workflow)
        koji_auth = {
            'proxyuser': koji_proxyuser,
            'ssl_certs_dir': koji_ssl_certs_dir,
            'krb_principal': koji_krb_principal,
            'krb_keytab': koji_krb_keytab,
        }
        # Remove empty values from auth dict to avoid login when not needed
        koji_auth = {k: v for k, v in koji_auth.items() if v}
        self.koji_info = {
            'hub': koji_hub,
            'root': koji_root,
            'auth': koji_auth or None
        }
        self.path_info = koji.PathInfo(topdir=self.koji_info['root'])
        self.allowed_domains = set(domain.lower() for domain in allowed_domains or [])
        self.workdir = self.workflow.source.get_dockerfile_path()[1]
        self.session = None

    def read_nvr_requests(self):
        file_path = os.path.join(self.workdir, self.NVR_REQUESTS_FILENAME)
        if not os.path.exists(file_path):
            self.log.debug('%s not found', self.NVR_REQUESTS_FILENAME)
            return []

        nvr_requests = util.read_yaml(file_path, 'schemas/fetch-artifacts-nvr.json')
        return [NvrRequest(**nvr_request) for nvr_request in nvr_requests]

    def read_url_requests(self):
        file_path = os.path.join(self.workdir, self.URL_REQUESTS_FILENAME)
        if not os.path.exists(file_path):
            self.log.debug('%s not found', self.URL_REQUESTS_FILENAME)
            return []

        return util.read_yaml(file_path, 'schemas/fetch-artifacts-url.json')

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
                             .format(self.NVR_REQUESTS_FILENAME, ', '.join(errors)))
        return download_queue

    def process_by_url(self, url_requests):
        download_queue = []
        errors = []

        for url_request in url_requests:
            url = url_request['url']

            if self.allowed_domains:
                parsed_file_url = urlparse(url.lower())
                file_url = parsed_file_url.netloc + parsed_file_url.path
                if not any(map(lambda prefix: file_url.startswith(prefix), self.allowed_domains)):
                    errors.append('File URL {} is not in list of allowed domains: {}'
                                  .format(file_url, self.allowed_domains))
                    continue

            checksums = {algo: url_request[algo] for algo in hashlib.algorithms_guaranteed
                         if algo in url_request}

            target = url_request.get('target', url.rsplit('/', 1)[-1])
            download_queue.append(DownloadRequest(url, target, checksums))

        if errors:
            raise ValueError('Errors found while processing {}: {}'
                             .format(self.URL_REQUESTS_FILENAME, ', '.join(errors)))

        return download_queue

    def download_files(self, downloads):
        artifacts_path = os.path.join(self.workdir, self.DOWNLOAD_DIR)

        self.log.debug('%d files to download', len(downloads))

        for index, download in enumerate(downloads):
            dest_path = os.path.join(artifacts_path, download.dest)
            dest_dir = dest_path.rsplit('/', 1)[0]
            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir)

            self.log.debug('%d/%d downloading %s', index + 1, len(downloads),
                           download.url)

            checksums = {algo: hashlib.new(algo) for algo in download.checksums}
            request = requests.get(download.url, stream=True)
            request.raise_for_status()
            with open(dest_path, 'wb') as f:
                for chunk in request.iter_content():
                    f.write(chunk)
                    for checksum in checksums.values():
                        checksum.update(chunk)

            for algo, checksum in checksums.items():
                if checksum.hexdigest() != download.checksums[algo]:
                    raise ValueError(
                        'Computed {} checksum, {}, does not match expected checksum, {}'
                        .format(algo, checksum.hexdigest(), download.checksums[algo]))

    def run(self):
        self.session = create_koji_session(self.koji_info['hub'], self.koji_info.get('auth'))

        nvr_requests = self.read_nvr_requests()
        url_requests = self.read_url_requests()

        download_queue = (self.process_by_nvr(nvr_requests) +
                          self.process_by_url(url_requests))

        self.download_files(download_queue)

        # TODO: Return a list of files for koji metadata
        return download_queue
