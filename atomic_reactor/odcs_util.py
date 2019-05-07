"""
Copyright (c) 2017, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import

from atomic_reactor.util import get_retrying_requests_session
from textwrap import dedent

import json
import logging
import time


logger = logging.getLogger(__name__)
MULTILIB_METHOD_DEFAULT = ['devel', 'runtime']


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

    def start_compose(self, source_type, source, packages=None, sigkeys=None, arches=None,
                      flags=None, multilib_arches=None, multilib_method=None,
                      modular_koji_tags=None):
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
        :param arches: list<str>, List of additional Koji arches to build this compose for.
                        By default, the compose is built only for "x86_64" arch.
        :param multilib_arches: list<str>, List of Koji arches to build as multilib in this
                        compose. By default, no arches are built as multilib.
        :param multilib_method: list<str>, list of methods to determine which packages should
                        be included in a multilib compose. Defaults to none, but the value
                        of ['devel', 'runtime] will be passed to ODCS if multilib_arches is
                        not empty and no mulitlib_method value is provided.
        :param modular_koji_tags: list<str>, the koji tags which are tagged to builds from the
                        modular Koji Content Generator.  Builds with matching tags will be
                        included in the compose.

        :return: dict, status of compose being created by request.
        """
        body = {
            'source': {
                'type': source_type,
                'source': source
            }
        }
        if source_type == "tag":
            body['source']['packages'] = packages or []

        if sigkeys is not None:
            body['source']['sigkeys'] = sigkeys

        if flags is not None:
            body['flags'] = flags

        if arches is not None:
            body['arches'] = arches

        if multilib_arches:
            body['multilib_arches'] = multilib_arches
            body['multilib_method'] = multilib_method or MULTILIB_METHOD_DEFAULT

        if source_type == "module" and modular_koji_tags:
            body['modular_koji_tags'] = modular_koji_tags

        logger.info("Starting compose: %s", body)
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
                         timeout=3600):
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
                state_reason = response_json.get('state_reason', 'Unknown')
                logger.error(dedent("""\
                   Compose %s failed: %s
                   Details: %s
                   """), compose_id, state_reason, json.dumps(response_json, indent=4))
                raise RuntimeError('Failed request for compose_id={}: {}'
                                   .format(compose_id, state_reason))

            if response_json['state_name'] not in ['wait', 'generating']:
                logger.debug("Retrieved compose information for compose_id={}: {}"
                             .format(compose_id, json.dumps(response_json, indent=4)))
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
