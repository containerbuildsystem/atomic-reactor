"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import hashlib
import os
import re

from atomic_reactor.util import get_retrying_requests_session


class PNCUtil(object):

    def __init__(self, pnc_map, session=None):
        if not session:
            self.session = get_retrying_requests_session()
        else:
            self.session = session

        self.pnc_map = pnc_map
        self.base_api_url = self.pnc_map['base_api_url']
        self._artifact_request_url = None
        self._scm_archive_request_url = None

    @property
    def artifact_request_url(self):
        if not self._artifact_request_url:
            # using urljoin here causes the API path in the base_api_url to be removed
            self._artifact_request_url = self.base_api_url + '/' + self.pnc_map['get_artifact_path']
        return self._artifact_request_url

    @property
    def scm_archive_request_url(self):
        if not self._scm_archive_request_url:
            # using urljoin here causes the API path in the base_api_url to be removed
            self._scm_archive_request_url = (self.base_api_url + '/'
                                             + self.pnc_map['get_scm_archive_path'])
        return self._scm_archive_request_url

    def get_artifact(self, artifact_id):
        """
        Return a URL to artifact and it's checksums
        :param artifact_id: str PNC artifact id
        :return str, dict; URL of the artifacts and it's checksums to verify download
        :rtype str, dict; URL and checksums
        """
        response = self.session.get(self.artifact_request_url.format(artifact_id))
        response.raise_for_status()

        artifact = response.json()
        url = artifact['publicUrl']
        checksums = {algo: artifact[algo] for algo in hashlib.algorithms_guaranteed
                     if algo in artifact}

        return url, checksums

    def get_scm_archive_from_build_id(self, build_id: str):
        """
        Return a scm archive URL and filename from PNC REST API.
        :param build_id: str PNC build id
        :return str, str URL and filename of the scm archive
        :rtype str, str, URL and filename
        """

        # This endpoint for the API is redirected to the actual source URL which
        #  we don't want to download yet, so we're disabling the redirect to just
        #  get the redirect location in response header
        response = self.session.get(self.scm_archive_request_url.format(build_id),
                                    allow_redirects=False)
        response.raise_for_status()

        url = response.headers.get('Location')
        dest_filename = os.path.basename(url)

        # Often the SCM URL will be gerrit URL which do no have the filename in URL
        # So we'll have to get the filename from the header if it's not valid.
        if not re.fullmatch(r'^[\w\-.]+$', dest_filename):
            dest_filename = self.session.head(url).headers.get(
                "Content-disposition").split("filename=")[1].replace('"', '')

        return url, dest_filename
