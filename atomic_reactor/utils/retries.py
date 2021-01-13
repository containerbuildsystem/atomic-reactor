"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from atomic_reactor.constants import (HTTP_CLIENT_STATUS_RETRY,
                                      HTTP_MAX_RETRIES,
                                      HTTP_BACKOFF_FACTOR,
                                      HTTP_REQUEST_TIMEOUT,
                                      HTTP_CONNECTION_ERROR_RETRIES)


class SessionWithTimeout(requests.Session):
    """
    requests Session with added timeout
    """
    def __init__(self, *args, **kwargs):
        super(SessionWithTimeout, self).__init__(*args, **kwargs)

    def request(self, *args, **kwargs):
        kwargs.setdefault('timeout', HTTP_REQUEST_TIMEOUT)
        return super(SessionWithTimeout, self).request(*args, **kwargs)


# This is a hook to mock during tests to temporarily disable retries
def _http_retries_disabled():
    return False


def get_retrying_requests_session(client_statuses=HTTP_CLIENT_STATUS_RETRY,
                                  times=HTTP_MAX_RETRIES, connect=HTTP_CONNECTION_ERROR_RETRIES,
                                  delay=HTTP_BACKOFF_FACTOR, method_whitelist=None,
                                  raise_on_status=True):
    if _http_retries_disabled():
        times = 0

    retry = Retry(
        total=int(times),
        connect=connect,
        backoff_factor=delay,
        status_forcelist=client_statuses,
        method_whitelist=method_whitelist
    )

    # raise_on_status was added later to Retry, adding compatibility to work
    # with newer versions and ignoring this option with older ones
    if hasattr(retry, 'raise_on_status'):
        retry.raise_on_status = raise_on_status

    session = SessionWithTimeout()
    session.mount('http://', HTTPAdapter(max_retries=retry))
    session.mount('https://', HTTPAdapter(max_retries=retry))

    return session
