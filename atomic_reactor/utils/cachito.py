"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import

from textwrap import dedent
import json
import logging
import requests
import time

from atomic_reactor.constants import REMOTE_SOURCES_FILENAME
from atomic_reactor.download import download_url
from atomic_reactor.util import get_retrying_requests_session


logger = logging.getLogger(__name__)


class CachitoAPIError(Exception):
    """Top level exception for errors in interacting with Cachito's API"""


class CachitoAPIInvalidRequest(CachitoAPIError):
    """Invalid request made to Cachito's API"""


class CachitoAPIUnsuccessfulRequest(CachitoAPIError):
    """Cachito's API request not completed successfully"""


class CachitoAPIRequestTimeout(CachitoAPIError):
    """A request to Cachito's API took too long to complete"""


class CachitoAPI(object):

    def __init__(self, api_url, insecure=False, cert=None):
        self.api_url = api_url
        self.session = self._make_session(insecure=insecure, cert=cert)

    def _make_session(self, insecure, cert):
        # method_whitelist=False allows retrying non-idempotent methods like POST
        session = get_retrying_requests_session(method_whitelist=False)
        session.verify = not insecure
        if cert:
            session.cert = cert
        return session

    def request_sources(self, repo, ref, flags=None, pkg_managers=None, user=None,
                        dependency_replacements=None):
        """Start a new Cachito request

        :param repo: str, the URL to the SCM repository
        :param ref: str, the SCM reference to fetch
        :param flags: list<str>, list of flag names
        :param pkg_managers: list<str>, list of package managers to be used for resolving
                             dependencies
        :param user: str, user the request is created on behalf of. This is reserved for privileged
                     users that can act as cachito representatives
        :param dependency_replacements: list<dict>, dependencies to be replaced by cachito

        :return: dict, representation of the created Cachito request
        :raise CachitoAPIInvalidRequest: if Cachito determines the request is invalid
        """
        payload = {
            'repo': repo,
            'ref': ref,
            'flags': flags,
            'pkg_managers': pkg_managers,
            'user': user,
            'dependency_replacements': dependency_replacements,
        }
        # Remove None values
        payload = {k: v for k, v in payload.items() if v}

        url = '{}/api/v1/requests'.format(self.api_url)
        logger.debug('Making request %s with payload:\n%s', url, json.dumps(payload, indent=4))
        response = self.session.post(url, json=payload)

        try:
            response_json = response.json()
            logger.debug('Cachito response:\n%s', json.dumps(response_json, indent=4))
        except ValueError:  # json.JSONDecodeError in py3 (is a subclass of ValueError)
            response_json = None

        if response.status_code == requests.codes.bad_request:
            raise CachitoAPIInvalidRequest(response_json['error'])
        response.raise_for_status()
        return response_json

    def wait_for_request(
            self, request, burst_retry=1, burst_length=30, slow_retry=10, timeout=3600):
        """Wait for a Cachito request to complete

        :param request: int or dict, either the Cachito request ID or a dict with 'id' key
        :param burst_retry: int, seconds to wait between retries prior to exceeding
                            the burst length
        :param burst_length: int, seconds to switch to slower retry period
        :param slow_retry: int, seconds to wait between retries after exceeding
                           the burst length
        :param timeout: int, when to give up waiting for compose request

        :return: dict, latest representation of the Cachito request
        :raise CachitoAPIUnsuccessfulRequest: if the request completes unsuccessfully
        :raise CachitoAPIRequestTimeout: if the request does not complete timely
        """
        request_id = self._get_request_id(request)
        url = '{}/api/v1/requests/{}'.format(self.api_url, request_id)
        logger.info('Waiting for request %s to complete...', request_id)

        start_time = time.time()
        while True:
            response = self.session.get(url)
            response.raise_for_status()
            response_json = response.json()

            state = response_json['state']
            if state in ('stale', 'failed'):
                state_reason = response_json.get('state_reason') or 'Unknown'
                logger.error(dedent("""\
                   Request %s is in "%s" state: %s
                   Details: %s
                   """), request_id, state, state_reason, json.dumps(response_json, indent=4))
                raise CachitoAPIUnsuccessfulRequest(
                   'Request {} is in "{}" state: {}'.format(request_id, state, state_reason))

            if state == 'complete':
                logger.debug(dedent("""\
                    Request %s is complete
                    Details: %s
                    """), request_id, json.dumps(response_json, indent=4))
                return response_json

            # All other states are expected to be transient and are not checked.

            elapsed = time.time() - start_time
            if elapsed > timeout:
                logger.error(dedent("""\
                    Request %s not completed after %s seconds
                    Details: %s
                    """), url, timeout, json.dumps(response_json, indent=4))
                raise CachitoAPIRequestTimeout(
                    'Request %s not completed after %s seconds' % (url, timeout))
            else:
                if elapsed > burst_length:
                    time.sleep(slow_retry)
                else:
                    time.sleep(burst_retry)

    def download_sources(self, request, dest_dir='.', dest_filename=REMOTE_SOURCES_FILENAME):
        """Download the sources from a Cachito request

        :param request: int or dict, either the Cachito request ID or a dict with 'id' key
        :param dest_dir: str, existing directory to create file in
        :param dest_filename: str, optional filename for downloaded file
        """
        request_id = self._get_request_id(request)
        logger.debug('Downloading sources bundle from request %ds', request_id)
        url = self.assemble_download_url(request_id)
        dest_path = download_url(
            url, dest_dir=dest_dir, insecure=not self.session.verify, session=self.session,
            dest_filename=dest_filename)
        logger.debug('Sources bundle for request %d downloaded to %s', request_id, dest_path)
        return dest_path

    def assemble_download_url(self, request):
        """Return the URL to be used for downloading the sources from a Cachito request

        :param request: int or dict, either the Cachito request ID or a dict with 'id' key

        :return: str, the URL to download the sources
        """
        request_id = self._get_request_id(request)
        return '{}/api/v1/requests/{}/download'.format(self.api_url, request_id)

    def get_request_config(self, request):
        """Get the configuration files associated with a request

        :param request: int or dict, either the Cachito request ID or a dict with 'id' key

        :return: list<dict>, configuration data for the given request.
                 entries include path, type, and content
        """
        request_id = self._get_request_id(request)
        logger.debug('Retrieving configuration files for request %ds', request_id)
        url = '{}/api/v1/requests/{}/configuration-files'.format(self.api_url, request_id)
        response = self.session.get(url)
        response_json = response.json()
        response.raise_for_status()
        return response_json

    def _get_request_id(self, request):
        if isinstance(request, int):
            return request
        elif isinstance(request, dict):
            return request['id']
        raise ValueError('Unexpected request type: {}'.format(request))


if __name__ == '__main__':
    logging.basicConfig()
    logger.setLevel(logging.DEBUG)

    # See instructions on how to start a local instance of Cachito:
    #   https://github.com/release-engineering/cachito
    api = CachitoAPI('http://localhost:8080', insecure=True)
    response = api.request_sources(
        'https://github.com/release-engineering/retrodep.git',
        'e1be527f39ec31323f0454f7d1422c6260b00580',
    )
    request_id = response['id']
    api.wait_for_request(request_id)
    api.download_sources(request_id)
