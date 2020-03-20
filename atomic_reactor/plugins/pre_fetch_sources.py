"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import absolute_import

import os
import tempfile

import koji
from six import string_types

from atomic_reactor.constants import PLUGIN_FETCH_SOURCES_KEY
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_reactor_config import (
    get_koji,
    get_koji_path_info,
    get_koji_session,
    get_config,
    get_source_container,
    NO_FALLBACK
)
from atomic_reactor.util import get_retrying_requests_session
from atomic_reactor.download import download_url
from atomic_reactor.metadata import label_map


@label_map('sources_for_nvr')
class FetchSourcesPlugin(PreBuildPlugin):
    """Download sources that may be used in further steps to compose Source Containers"""
    key = PLUGIN_FETCH_SOURCES_KEY
    is_allowed_to_fail = False
    SRPMS_DOWNLOAD_DIR = 'image_sources'
    REMOTE_SOUCES_DOWNLOAD_DIR = 'remote_sources'

    def __init__(
        self, tasker, workflow, koji_build_id=None, koji_build_nvr=None, signing_intent=None,
    ):
        """
        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param koji_build_id: int, container image koji build id
        :param koji_build_nvr: str, container image koji build NVR
        :param signing_intent: str, ODCS signing intent name
        """
        if not koji_build_id and not koji_build_nvr:
            err_msg = ('{} expects either koji_build_id or koji_build_nvr to be defined'
                       .format(self.__class__.__name__))
            raise TypeError(err_msg)
        type_errors = []
        if koji_build_id is not None and not isinstance(koji_build_id, int):
            type_errors.append('koji_build_id must be an int. Got {}'.format(type(koji_build_id)))
        if koji_build_nvr is not None and not isinstance(koji_build_nvr, string_types):
            type_errors.append('koji_build_nvr must be a str. Got {}'
                               .format(type(koji_build_nvr)))
        if type_errors:
            raise TypeError(type_errors)

        super(FetchSourcesPlugin, self).__init__(tasker, workflow)
        self.koji_build = None
        self.koji_build_id = koji_build_id
        self.koji_build_nvr = koji_build_nvr
        self.signing_intent = signing_intent
        self.session = get_koji_session(self.workflow, NO_FALLBACK)
        self.pathinfo = get_koji_path_info(self.workflow, NO_FALLBACK)

    def run(self):
        """
        :return: dict, binary image koji build id and nvr, and path to directory with
        downloaded sources
        """
        self.set_koji_image_build_data()
        signing_intent = self.get_signing_intent()
        koji_config = get_koji(self.workflow, {})
        insecure = koji_config.get('insecure_download', False)
        urls = self.get_srpm_urls(signing_intent['keys'], insecure=insecure)
        urls_remote = self.get_remote_urls()

        if not urls and not urls_remote:
            msg = "No srpms or remote sources found for source container," \
                  " would produce empty source container image"
            self.log.error(msg)
            raise RuntimeError(msg)

        sources_dir = None
        remote_sources_dir = None
        if urls:
            sources_dir = self.download_sources(urls, insecure=insecure)
        if urls_remote:
            remote_sources_dir = self.download_sources(urls_remote, insecure=insecure,
                                                       download_dir=self.REMOTE_SOUCES_DOWNLOAD_DIR)
        return {
                'sources_for_koji_build_id': self.koji_build_id,
                'sources_for_nvr': self.koji_build_nvr,
                'image_sources_dir': sources_dir,
                'remote_sources_dir': remote_sources_dir,
                'signing_intent': self.signing_intent,
        }

    def download_sources(self, sources, insecure=False, download_dir=SRPMS_DOWNLOAD_DIR):
        """Download sources content

        Download content in the given URLs into a new temporary directory and
        return a list with each downloaded artifact's path.

        :param sources: list, dicts with URLs to download
        :param insecure: bool, whether to perform TLS checks of urls
        :param download_dir: str, directory where to download content
        :return: str, paths to directory with downloaded sources
        """
        workdir = tempfile.mkdtemp()
        dest_dir = os.path.join(workdir, download_dir)
        if not os.path.exists(dest_dir):
            os.makedirs(dest_dir)

        req_session = get_retrying_requests_session()
        for source in sources:
            download_url(source['url'], dest_dir, insecure=insecure,
                         session=req_session, dest_filename=source.get('dest'))

        return dest_dir

    def set_koji_image_build_data(self):
        build_identifier = self.koji_build_nvr or self.koji_build_id

        # strict means this raises a koji.GenericError informing no matching build was found in
        # case the build does not exist
        self.koji_build = self.session.getBuild(build_identifier, strict=True)

        if self.koji_build_id and (self.koji_build_id != self.koji_build['build_id']):
            err_msg = (
                'koji_build_id {} does not match koji_build_nvr {} with id {}. '
                'When specifying both an id and an nvr, they should point to the same image build'
                .format(self.koji_build_id, self.koji_build_nvr, self.koji_build['build_id'])
                )
            raise ValueError(err_msg)

        build_extras = self.koji_build['extra']
        if 'image' not in build_extras:
            err_msg = ('koji build {} is not image build which source container requires'.
                       format(self.koji_build['nvr']))
            raise ValueError(err_msg)

        elif 'sources_for_nvr' in self.koji_build['extra']['image']:
            err_msg = ('koji build {} is source container build, source container can not '
                       'use source container build image'.format(self.koji_build['nvr']))
            raise ValueError(err_msg)

        if not self.koji_build_id:
            self.koji_build_id = self.koji_build['build_id']
        if not self.koji_build_nvr:
            self.koji_build_nvr = self.koji_build['nvr']

    def assemble_srpm_url(self, base_url, srpm_filename, sign_key=None):
        """Assemble the URL used to fetch an SRPM file

        :param base_url: str, Koji root base URL with the given build artifacts
        :param srpm_filename: str, name of the SRPM file
        :param sign_key: str, key used to sign the SRPM, as listed in the signing intent
        :return: list, strings with URLs pointing to SRPM files
        """
        srpm_info = koji.parse_NVRA(srpm_filename)
        if sign_key:
            srpm_path = self.pathinfo.signed(srpm_info, sign_key)
        else:
            srpm_path = self.pathinfo.rpm(srpm_info)
        return '/'.join([base_url, srpm_path])

    def _get_remote_urls_helper(self, koji_build):
        """Fetch remote source urls from specific build

        :param koji_build: dict, koji build
        :return: str, URL pointing to remote sources
        """
        self.log.debug('get remote_urls: %s', koji_build['build_id'])
        archives = self.session.listArchives(koji_build['build_id'], type='remote-sources')
        self.log.debug('archives: %s', archives)
        remote_sources_path = self.pathinfo.typedir(koji_build, btype='remote-sources')
        remote_sources_urls = []

        for archive in archives:
            if archive['type_name'] == 'tar':
                remote_source = {}
                remote_source['url'] = os.path.join(remote_sources_path, archive['filename'])
                remote_source['dest'] = '-'.join([koji_build['nvr'], archive['filename']])
                remote_sources_urls.append(remote_source)
        return remote_sources_urls

    def get_remote_urls(self):
        """Fetch remote source urls from all builds

        :return: list, dicts with URL pointing to remote sources
        """
        remote_sources_urls = []

        remote_sources_urls.extend(self._get_remote_urls_helper(self.koji_build))

        koji_build = self.koji_build

        while 'parent_build_id' in koji_build['extra']['image']:
            koji_build = self.session.getBuild(koji_build['extra']['image']['parent_build_id'],
                                               strict=True)
            remote_sources_urls.extend(self._get_remote_urls_helper(koji_build))

        return remote_sources_urls

    def get_srpm_urls(self, sigkeys=None, insecure=False):
        """Fetch SRPM download URLs for each image generated by a build

        Build each possible SRPM URL and check if the URL is available,
        respecting the signing intent preference order.

        :param sigkeys: list, strings for keys which signed the srpms to be fetched
        :return: list, strings with URLs pointing to SRPM files
        """
        if not sigkeys:
            sigkeys = ['']

        self.log.debug('get srpm_urls: %s', self.koji_build_id)
        archives = self.session.listArchives(self.koji_build_id, type='image')
        self.log.debug('archives: %s', archives)
        rpms = [rpm for archive in archives
                for rpm in self.session.listRPMs(imageID=archive['id'])]

        src_config = get_source_container(self.workflow, fallback={})
        blacklist_srpms = src_config.get('blacklist_srpms', [])

        srpm_build_paths = {}
        for rpm in rpms:
            rpm_id = rpm['id']
            self.log.debug('Resolving SRPM for RPM ID: %s', rpm_id)

            if rpm['external_repo_name'] != 'INTERNAL':
                msg = ('RPM comes from an external repo (RPM ID: {}). '
                       'External RPMs are currently not supported.').format(rpm_id)
                raise RuntimeError(msg)

            rpm_hdr = self.session.getRPMHeaders(rpm_id, headers=['SOURCERPM'])
            if 'SOURCERPM' not in rpm_hdr:
                raise RuntimeError('Missing SOURCERPM header (RPM ID: {})'.format(rpm_id))

            srpm_name = rpm_hdr['SOURCERPM'].rsplit('-', 2)[0]

            if any(black == srpm_name for black in blacklist_srpms):
                self.log.debug('skipping blacklisted srpm %s', rpm_hdr['SOURCERPM'])
                continue

            srpm_filename = rpm_hdr['SOURCERPM']
            if srpm_filename in srpm_build_paths:
                continue
            rpm_build = self.session.getBuild(rpm['build_id'], strict=True)
            base_url = self.pathinfo.build(rpm_build)
            srpm_build_paths[srpm_filename] = base_url

        srpm_urls = []
        missing_srpms = []
        req_session = get_retrying_requests_session()
        for srpm_filename, base_url in srpm_build_paths.items():
            for sigkey in sigkeys:
                # koji uses lowercase for paths. We make sure the sigkey is in lower case
                url_candidate = self.assemble_srpm_url(base_url, srpm_filename, sigkey.lower())
                request = req_session.head(url_candidate, verify=not insecure)
                if request.ok:
                    srpm_urls.append({'url': url_candidate})
                    self.log.debug('%s is available for signing key "%s"', srpm_filename, sigkey)
                    break

            else:
                self.log.error('%s not found for the given signing intent: %s"', srpm_filename,
                               self.signing_intent)
                missing_srpms.append(srpm_filename)

        if missing_srpms:
            raise RuntimeError('Could not find files signed by any of {} for these SRPMS: {}'
                               .format(sigkeys, missing_srpms))

        return srpm_urls

    def get_signing_intent(self):
        """Get the signing intent to be used to fetch files from Koji

        :return: dict, signing intent object as per atomic_reactor/schemas/config.json
        """
        odcs_config = get_config(self.workflow).get_odcs_config()
        if odcs_config is None:
            self.log.warning('No ODCS configuration available. Allowing unsigned SRPMs')
            return {'keys': None}

        if not self.signing_intent:
            try:
                self.signing_intent = self.koji_build['extra']['image']['odcs']['signing_intent']
            except (KeyError, TypeError):
                self.log.debug('Image koji build, %s(%s), does not define signing_intent.',
                               self.koji_build_nvr, self.koji_build_id)
                self.signing_intent = odcs_config.default_signing_intent

        signing_intent = odcs_config.get_signing_intent_by_name(self.signing_intent)
        return signing_intent
