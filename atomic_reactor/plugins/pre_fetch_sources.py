"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import absolute_import

import os
import tempfile
import time

import requests
from six.moves.urllib.parse import urlparse

from atomic_reactor.constants import (
    DEFAULT_DOWNLOAD_BLOCK_SIZE,
    HTTP_BACKOFF_FACTOR,
    HTTP_MAX_RETRIES,
    PLUGIN_FETCH_SOURCES_KEY
)
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_reactor_config import (
    get_koji,
    get_koji_path_info,
    get_koji_session,
    get_config,
    NO_FALLBACK
)
from atomic_reactor.util import get_retrying_requests_session


class FetchSourcesPlugin(PreBuildPlugin):
    """Download sources that may be used in further steps to compose Source Containers"""
    key = PLUGIN_FETCH_SOURCES_KEY
    is_allowed_to_fail = False
    DOWNLOAD_DIR = 'image_sources'

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
        if koji_build_nvr is not None and not isinstance(koji_build_nvr, str):
            type_errors.append('koji_build_nvr must be a str. Got {}'.format(type(koji_build_nvr)))
        if type_errors:
            raise TypeError(type_errors)

        super(FetchSourcesPlugin, self).__init__(tasker, workflow)
        self.koji_build = None
        self.koji_build_id = koji_build_id
        self.koji_build_nvr = koji_build_nvr
        self.signing_intent = signing_intent
        self.fallback_koji = get_koji(self.workflow, NO_FALLBACK)
        self.session = get_koji_session(self.workflow, self.fallback_koji, instance='sources')

    def run(self):
        """
        :return: dict, binary image koji build id and nvr, and path to directory with
        downloaded sources
        """
        self.set_koji_image_build_data()
        signing_intent = self.get_signing_intent()
        koji_config = get_koji(self.workflow, self.fallback_koji, instance='sources')
        insecure = koji_config.get('insecure_download', False)
        urls = self.get_srpm_urls(signing_intent['keys'], insecure=insecure)
        sources_dir = self.download_sources(urls, insecure=insecure)
        return {
                'sources_for_koji_build_id': self.koji_build_id,
                'sources_for_nvr': self.koji_build_nvr,
                'image_sources_dir': sources_dir,
        }

    def download_sources(self, urls, insecure=False):
        """Download sources content

        Download content in the given URLs into a new temporary directory and
        return a list with each downloaded artifact's path.

        :param urls: int, Koji build id of the container image we want SRPMs for
        :param insecure: bool, whether to perform TLS checks of urls
        :return: str, paths to directory with downloaded sources
        """
        workdir = tempfile.mkdtemp()
        dest_dir = os.path.join(workdir, self.DOWNLOAD_DIR)
        if not os.path.exists(dest_dir):
            os.makedirs(dest_dir)

        req_session = get_retrying_requests_session()
        for url in urls:
            parsed_url = urlparse(url)
            dest_filename = os.path.basename(parsed_url.path)
            dest_path = os.path.join(dest_dir, dest_filename)
            self.log.debug('Downloading %s', url)

            for attempt in range(HTTP_MAX_RETRIES + 1):
                request = req_session.get(url, stream=True, verify=not insecure)
                request.raise_for_status()
                try:
                    with open(dest_path, 'wb') as f:
                        for chunk in request.iter_content(chunk_size=DEFAULT_DOWNLOAD_BLOCK_SIZE):
                            f.write(chunk)
                    break
                except (requests.exceptions.RequestException):
                    if attempt < HTTP_MAX_RETRIES:
                        time.sleep(HTTP_BACKOFF_FACTOR * (2 ** attempt))
                    else:
                        raise

            self.log.debug('Download finished: %s', dest_path)

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
        url_components = [base_url, 'src', srpm_filename]
        if sign_key:
            url_components[1:1] = ['data', 'signed', sign_key]
        return '/'.join(url_components)

    def get_srpm_urls(self, sigkeys=None, insecure=False):
        """Fetch SRPM download URLs for each image generated by a build

        Build each possible SRPM URL and check if the URL is available,
        respecting the signing intent preference order.

        :param sigkeys: list, strings for keys which signed the srpms to be fetched
        :return: list, strings with URLs pointing to SRPM files
        """
        if not sigkeys:
            sigkeys = ['']

        archives = self.session.listArchives(self.koji_build_id, type='image')
        rpm_build_ids = {rpm['id']: rpm['build_id'] for archive in archives
                         for rpm in self.session.listRPMs(imageID=archive['id'])}

        srpm_build_paths = {}
        path_info = get_koji_path_info(self.workflow, self.fallback_koji, instance='sources')
        for rpm_id, rpm_build_id in rpm_build_ids.items():
            rpm_hdr = self.session.getRPMHeaders(rpm_id, headers=['SOURCERPM'])
            srpm_filename = rpm_hdr['SOURCERPM']
            if srpm_filename in srpm_build_paths:
                continue
            rpm_build = self.session.getBuild(rpm_build_id, strict=True)
            base_url = path_info.build(rpm_build)
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
                    srpm_urls.append(url_candidate)
                    break
                self.log.debug('%s not found for signing key "%s" at %s (returned %s)',
                               srpm_filename, sigkey, url_candidate, request.status_code)

            else:
                missing_srpms.append(srpm_filename)

        if missing_srpms:
            raise RuntimeError('Could not find files signed by any of {} for these SRPMS: {}'
                               .format(sigkeys, srpm_filename))

        return srpm_urls

    def get_signing_intent(self):
        """Get the signing intent to be used to fetch files from Koji

        :return: dict, signing intent object as per atomic_reactor/schemas/config.json
        """
        odcs_config = get_config(self.workflow).get_odcs_config()
        if not self.signing_intent:
            try:
                self.signing_intent = self.koji_build['extra']['image']['odcs']['signing_intent']
            except (KeyError, TypeError):
                self.log.debug('Image koji build, %s(%s), does not define signing_intent.',
                               self.koji_build_nvr, self.koji_build_id)
                self.signing_intent = odcs_config.default_signing_intent

        signing_intent = odcs_config.get_signing_intent_by_name(self.signing_intent)
        return signing_intent
