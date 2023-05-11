"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from io import BufferedReader, BytesIO
import os
import requests
import responses
import tempfile
import time

import pytest
from flexmock import flexmock

from atomic_reactor.util import get_retrying_requests_session
from atomic_reactor.download import download_url
from atomic_reactor.constants import CACHITO_ALG_STR


class TestDownloadUrl(object):
    @responses.activate
    def test_happy_path(self):
        url = 'https://example.com/path/file'
        dest_dir = tempfile.mkdtemp()
        content = b'abc'
        reader = BufferedReader(BytesIO(content), buffer_size=1)
        responses.add(responses.GET, url, body=reader)
        result = download_url(url, dest_dir)

        assert os.path.basename(result) == 'file'
        with open(result, 'rb') as f:
            assert f.read() == content

    @responses.activate
    def test_cachito_download_digest_matches(self):
        url = 'https://example.com/path/file'
        dest_dir = tempfile.mkdtemp()
        content = b'abc'
        digest = 'ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0='
        digest_str = f'{CACHITO_ALG_STR}={digest}'

        reader = BufferedReader(BytesIO(content), buffer_size=1)
        responses.add(responses.GET, url, body=reader, headers={'Digest': digest_str})
        result = download_url(url, dest_dir, verify_cachito_digest=True)

        assert os.path.basename(result) == 'file'
        with open(result, 'rb') as f:
            assert f.read() == content

    @responses.activate
    def test_cachito_download_digest_mismatches(self):
        url = 'https://example.com/path/file'
        dest_dir = tempfile.mkdtemp()
        content = b'abc'
        digest = 'wrong'
        digest_str = f'{CACHITO_ALG_STR}={digest}'

        reader = BufferedReader(BytesIO(content), buffer_size=1)
        responses.add(responses.GET, url, body=reader, headers={'Digest': digest_str})

        with pytest.raises(ValueError, match='does not match expected digest'):
            download_url(url, dest_dir, verify_cachito_digest=True)

    def test_connection_failure(self):
        url = 'https://example.com/path/file'
        dest_dir = tempfile.mkdtemp()
        session = get_retrying_requests_session()
        (flexmock(session)
         .should_receive('get')
         .and_raise(requests.exceptions.RetryError))
        with pytest.raises(requests.exceptions.RetryError):
            download_url(url, dest_dir, session=session)

    def test_streaming_failure(self):
        url = 'https://example.com/path/file'
        dest_dir = tempfile.mkdtemp()
        session = get_retrying_requests_session()
        # get response shows successful connection
        response = flexmock()
        (response
         .should_receive('raise_for_status'))
        # but streaming from the response fails
        (response
         .should_receive('iter_content')
         .and_raise(requests.exceptions.RequestException))
        # get on the session should return our mock response
        (flexmock(session)
         .should_receive('get')
         .and_return(response))
        # Speed through the retries
        (flexmock(time)
         .should_receive('sleep'))
        with pytest.raises(requests.exceptions.RequestException):
            download_url(url, dest_dir, session=session)
