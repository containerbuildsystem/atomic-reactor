"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import absolute_import

import logging
import os

import requests

from atomic_reactor.util import get_retrying_requests_session


class OMPSError(Exception):
    def __init__(self, msg, status_code=None, response=None):
        super(OMPSError, self).__init__(msg)
        self.status_code = status_code
        self.response = response


class OMPS(object):
    """Implementation of OMPS REST API calls"""

    @classmethod
    def from_config(cls, config):
        """Initialize instance from reactor config map"""
        with open(os.path.join(config['omps_secret_dir'], 'token'), 'r') as f:
            token = f.read().strip()
        return cls(
            config['omps_url'],
            config['omps_namespace'],
            token,
            insecure=config.get('insecure', False)
        )

    def __init__(self, url, organization, token, insecure=False):
        """
        :param url: URL of OMPS service
        :param organization: organization to be used for manifests
        :param token: secret auth token
        :param insecure: don't validate OMPS server cert
        """
        self._url = url
        self._organization = organization
        self._token = token
        self._insecure = insecure
        self.log = logging.getLogger(self.__class__.__name__)
        self.req_session = get_retrying_requests_session()

    @property
    def organization(self):
        return self._organization

    @property
    def url(self):
        return self._url

    def _handle_error(self, response):
        if response.status_code != requests.codes.ok:
            try:
                response_json = response.json()
            except Exception:
                response_json = {}
            self.log.debug(
                "OMPS returned status code %s: %s", response.status_code, response_json)

            error = response_json.get('error', 'unknown')

            # provide list of validation errors (if available) to users
            # so they don't have to inspect logs
            validation_info = response_json.get('validation_info', {})
            self.log.error("Operator manifests are invalid: %s", validation_info)
            msg = response_json.get('message', 'no details available')
            if validation_info:
                msg = "{} (validation errors: {})".format(msg, validation_info)

            raise OMPSError(
                "OMPS service request failed with error: {err}: {msg}".format(
                    err=error,
                    msg=msg
                ),
                status_code=response.status_code,
                response=response_json
            )

    def push_archive(self, fb):
        """Push operator manifest archive to appregistry via OMPS service

        :param fb: Binary file like object
        :raises OMPSError: when failure response is received
        :return: OMPS response
        """
        endpoint = '{url}/v2/{organization}/zipfile'.format(
            url=self.url, organization=self.organization)

        files = {'file': fb}
        headers = {'Authorization': self._token}

        self.log.debug("Pushing operator manifests via: %s", endpoint)
        r = self.req_session.post(
            endpoint, headers=headers, files=files,
            verify=not self._insecure
        )
        self._handle_error(r)

        self.log.debug("OMPS response - success: %s", r.json())
        return r.json()
