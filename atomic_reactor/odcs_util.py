"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.util import get_retrying_requests_session

import logging
import time


logger = logging.getLogger(__name__)


class ODCSClient(object):

    OIDC_TOKEN_HEADER = 'Authorization'
    OIDC_TOKEN_TYPE = 'Bearer'

    def __init__(self, url, insecure=False, token=None, cert=None):
        if url.endswith('/'):
            self.url = url
        else:
            self.url = url + '/'
        self._setup_session(insecure=insecure, token=token, cert=cert)

    def _setup_session(self, insecure, token, cert):
        # method_whitelist=False allows retrying non-idempotent methods like POST
        session = get_retrying_requests_session(method_whitelist=False)

        session.verify = not insecure

        if token:
            session.headers[self.OIDC_TOKEN_HEADER] = '%s %s' % (self.OIDC_TOKEN_TYPE, token)

        if cert:
            session.cert = cert

        self.session = session

    def start_compose(self, source_type, source, packages=None, sigkeys=None):
        """Start a new ODCS compose

        :param source_type: str, the type of compose to request (tag, module, pulp)
        :param source: str, if source_type "tag" is used, the name of the Koji tag
                       to use when retrieving packages to include in compose;
                       if source_type "module", white-space separated NAME-STREAM or
                       NAME-STREAM-VERSION list of modules to include in compose;
                       if source_type "pulp", white-space separated list of context-sets
                       to include in compose
        :param packages: list<str>, packages which should be included in a compose. Only
                         relevant when source_type "tag" is used.
        :param sigkeys: list<str>, IDs of signature keys. Only packages signed by one of
                        these keys will be included in a compose.

        :return: dict, status of compose being created by request.
        """
        body = {
            'source': {
                'type': source_type,
                'source': source
            }
        }
        if packages:
            body['source']['packages'] = packages

        if sigkeys is not None:
            body['source']['sigkeys'] = sigkeys

        logger.info("Starting compose for source_type={source_type}, source={source}"
                    .format(source_type=source_type, source=source))
        response = self.session.post('{}composes/'.format(self.url),
                                     json=body)
        response.raise_for_status()

        return response.json()

    def renew_compose(self, compose_id):
        """Renew, or extend, existing compose

        If the compose has already been removed, ODCS creates a new compose.
        Otherwise, it extends the time_to_expire of existing compose. In most
        cases, caller should assume the compose ID will change.

        :param compose_id: int, compose ID to renew

        :return: dict, status of compose being renewed.
        """
        logger.info("Renewing compose %d", compose_id)
        response = self.session.patch('{}composes/{}'.format(self.url, compose_id))
        response.raise_for_status()
        response_json = response.json()
        compose_id = response_json['id']
        logger.info("Renewed compose is %d", compose_id)
        return response_json

    def wait_for_compose(self, compose_id,
                         burst_retry=1,
                         burst_length=30,
                         slow_retry=10,
                         timeout=1800):
        """Wait for compose request to finalize

        :param compose_id: int, compose ID to wait for
        :param burst_retry: int, seconds to wait between retries prior to exceeding
                            the burst length
        :param burst_length: int, seconds to switch to slower retry period
        :param slow_retry: int, seconds to wait between retries after exceeding
                           the burst length
        :param timeout: int, when to give up waiting for compose request

        :return: dict, updated status of compose.
        :raise RuntimeError: if state_name becomes 'failed'
        """
        logger.debug("Getting compose information for information for compose_id={}"
                     .format(compose_id))
        url = '{}composes/{}'.format(self.url, compose_id)
        start_time = time.time()
        while True:
            response = self.session.get(url)
            response.raise_for_status()
            response_json = response.json()

            if response_json['state_name'] == 'failed':
                raise RuntimeError('Failed request for compose_id={}: {!r}'
                                   .format(compose_id, response_json))

            if response_json['state_name'] not in ['wait', 'generating']:
                logger.debug("Retrieved compose information for compose_id={}: {!r}"
                             .format(compose_id, response_json))
                return response_json

            elapsed = time.time() - start_time
            if elapsed > timeout:
                raise RuntimeError("Retrieving %s timed out after %s seconds" %
                                   (url, timeout))
            else:
                logger.debug("Retrying request compose_id={}, elapsed_time={}"
                             .format(compose_id, elapsed))

                if elapsed > burst_length:
                    time.sleep(slow_retry)
                else:
                    time.sleep(burst_retry)
