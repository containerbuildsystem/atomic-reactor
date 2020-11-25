"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import logging
import os
import time
import requests
from urllib.parse import urlparse

from atomic_reactor.util import get_retrying_requests_session
from atomic_reactor.constants import (
    DEFAULT_DOWNLOAD_BLOCK_SIZE,
    HTTP_BACKOFF_FACTOR,
    HTTP_MAX_RETRIES,
)


logger = logging.getLogger(__name__)


def download_url(url, dest_dir, insecure=False, session=None, dest_filename=None):
    """Download file from URL, handling retries

    To download to a temporary directory, use:
      f = download_url(url, tempfile.mkdtemp())

    :param url: URL to download from
    :param dest_dir: existing directory to create file in
    :param insecure: bool, whether to perform TLS checks
    :param session: optional existing requests session to use
    :param dest_filename: optional filename for downloaded file
    :return: str, path of downloaded file
    """

    if session is None:
        session = get_retrying_requests_session()

    parsed_url = urlparse(url)
    if not dest_filename:
        dest_filename = os.path.basename(parsed_url.path)
    dest_path = os.path.join(dest_dir, dest_filename)
    logger.debug('downloading %s', url)

    for attempt in range(HTTP_MAX_RETRIES + 1):
        response = session.get(url, stream=True, verify=not insecure)
        response.raise_for_status()
        try:
            with open(dest_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=DEFAULT_DOWNLOAD_BLOCK_SIZE):
                    f.write(chunk)
            break
        except requests.exceptions.RequestException:
            if attempt < HTTP_MAX_RETRIES:
                time.sleep(HTTP_BACKOFF_FACTOR * (2 ** attempt))
            else:
                raise

    logger.debug('download finished: %s', dest_path)
    return dest_path
