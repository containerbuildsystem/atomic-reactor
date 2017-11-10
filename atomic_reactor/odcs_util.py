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


OIDC_TOKEN_HEADER = 'OIDC_access_token'
OIDC_CLAIM_SCOPE_HEADER = 'OIDC_CLAIM_scope'


class ODCSClient(object):
    def __init__(self, url, insecure=False, token=None):
        if url.endswith('/'):
            self.url = url
        else:
            self.url = url + '/'
        self.insecure = insecure
        self.token = token
        # method_whitelist=False allows retrying non-idempotent methods like POST
        self.session = get_retrying_requests_session(method_whitelist=False)

    def _auth_headers(self):
        headers = {}
        if self.token:
            headers[OIDC_TOKEN_HEADER] = self.token
            headers[OIDC_CLAIM_SCOPE_HEADER] = ('openid https://id.fedoraproject.org/scope/groups '
                                                'https://pagure.io/odcs/new-compose '
                                                'https://pagure.io/odcs/renew-compose '
                                                'https://pagure.io/odcs/delete-compose')
            # FIXME: passing the claim scope in the request doesn't make any sense,
            #   because we're not to be trusted for what scopes we've claimed, but
            #   match what the server wants.

        return headers

    def start_compose(self, source_type, source):
        body = {
            'source': {
                'type': source_type,
                'source': source
            }
        }

        logger.info("Starting compose for source_type={source_type}, source={source}"
                    .format(source_type=source_type, source=source))
        response = self.session.post(self.url + 'composes/',
                                     json=body,
                                     headers=self._auth_headers())
        response.raise_for_status()

        return response.json()

    def wait_for_compose(self, compose_id,
                         burst_retry=1,
                         burst_length=30,
                         slow_retry=10,
                         timeout=300):
        logger.debug("Getting compose information for information for compose_id={}"
                     .format(compose_id))
        url = self.url + 'composes/' + str(compose_id)
        headers = self._auth_headers()
        start_time = time.time()
        while True:
            response = self.session.get(url, headers=headers)
            response.raise_for_status()
            response_json = response.json()

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
