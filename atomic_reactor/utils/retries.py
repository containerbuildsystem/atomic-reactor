"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import logging
import subprocess
from typing import List

import backoff
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from atomic_reactor.constants import (HTTP_CLIENT_STATUS_RETRY,
                                      HTTP_MAX_RETRIES,
                                      HTTP_BACKOFF_FACTOR,
                                      HTTP_REQUEST_TIMEOUT,
                                      SUBPROCESS_MAX_RETRIES,
                                      SUBPROCESS_BACKOFF_FACTOR)

logger = logging.getLogger(__name__)


class SessionWithTimeout(requests.Session):
    """
    requests Session with added timeout
    """
    def __init__(self, *args, **kwargs):
        super(SessionWithTimeout, self).__init__(*args, **kwargs)

    # pylint: disable=signature-differs
    def request(self, *args, **kwargs):
        kwargs.setdefault('timeout', HTTP_REQUEST_TIMEOUT)
        return super(SessionWithTimeout, self).request(*args, **kwargs)


# This is a hook to mock during tests to temporarily disable retries
def _http_retries_disabled():
    return False


def hook_log_error_response_content(response, *args, **kwargs):
    """Hook function to log response content when not 200

    :param response: the requests Response object
    :type response: requests.Response
    """
    if 400 <= response.status_code <= 599:
        logger.debug(
            'HTTP %d response from %s: %s',
            response.status_code, response.url, response.content
        )


def get_retrying_requests_session(client_statuses=HTTP_CLIENT_STATUS_RETRY,
                                  times=HTTP_MAX_RETRIES, delay=HTTP_BACKOFF_FACTOR,
                                  allowed_methods=None, raise_on_status=True):
    if _http_retries_disabled():
        times = 0

    retry = Retry(
        total=int(times),
        backoff_factor=delay,
        status_forcelist=client_statuses,
        allowed_methods=allowed_methods
    )

    # raise_on_status was added later to Retry, adding compatibility to work
    # with newer versions and ignoring this option with older ones
    if hasattr(retry, 'raise_on_status'):
        retry.raise_on_status = raise_on_status

    session = SessionWithTimeout()
    session.mount('http://', HTTPAdapter(max_retries=retry))
    session.mount('https://', HTTPAdapter(max_retries=retry))
    session.hooks['response'] = [hook_log_error_response_content]

    return session


@backoff.on_exception(
    backoff.expo,
    subprocess.CalledProcessError,
    factor=SUBPROCESS_BACKOFF_FACTOR,
    max_tries=SUBPROCESS_MAX_RETRIES + 1,  # total tries is N retries + 1 initial attempt
    jitter=None,  # use deterministic backoff, do not apply random jitter
)
def run_cmd(cmd: List[str], cleanup_cmd: List[str] = None) -> bytes:
    """Run a subprocess command, retry on any non-zero exit status.

    Whenever an attempt fails, the stdout and stderr of the failed command will be logged.
    If all attempts fail, the raised exception will also provide the combined stdout and stderr
    in the `output` attribute.

    If a cleanup command is specified it'll be run on exception before retry.

    :return: bytes, the combined stdout and stderr (if any) of the command
    """
    logger.debug("Running %s", " ".join(cmd))

    try:
        process = subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        logger.warning(
            "%s failed:\nSTDOUT:\n%s\nSTDERR:\n%s",
            cmd[0],
            e.stdout.decode(),
            e.stderr.decode(),
        )
        if cleanup_cmd:
            try:
                logger.debug("Running %s", " ".join(cleanup_cmd))
                subprocess.run(cleanup_cmd, check=True, capture_output=True)
            except subprocess.CalledProcessError as c_e:
                logger.warning(
                    "Cleanup command: %s failed:\nSTDOUT:\n%s\nSTDERR:\n%s",
                    cleanup_cmd[0],
                    c_e.stdout.decode(),
                    c_e.stderr.decode(),
                )
        raise

    if process.stderr:
        logger.warning("%s STDERR:\n%s", cmd[0], process.stderr.decode())

    return process.stdout
