"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from io import BufferedReader, BytesIO

import pytest
import requests
import responses
from flexmock import flexmock

from atomic_reactor.util import get_retrying_requests_session
from atomic_reactor.utils.pnc import get_scm_archive_from_build_id


class TestGetSCMArchiveFromBuildID(object):
    @responses.activate
    def test_connection_filename_in_header(self):
        base_api_url = 'https://example.com/{}/scm_archive'
        build_id = '1234'
        filename = 'source.tar.gz'
        scm_url = f'https://code.example.com/{filename};sf=tgz'
        content = b'abc'
        reader = BufferedReader(BytesIO(content), buffer_size=1)
        responses.add(responses.GET, base_api_url.format(build_id), body=reader, status=302,
                      headers={'Location': scm_url})
        responses.add(responses.HEAD, scm_url, body='', status=200,
                      headers={'Content-disposition': f'filename="{filename}"'})
        url, dest_filename = get_scm_archive_from_build_id(base_api_url, build_id)

        assert url == scm_url
        assert dest_filename == filename

    @responses.activate
    def test_connection_filename_in_url(self):
        base_api_url = 'https://example.com/{}/scm_archive'
        build_id = '1234'
        filename = 'source.tar.gz'
        scm_url = f'https://code.example.com/{filename}'

        responses.add(responses.GET, base_api_url.format(build_id), body='', status=302,
                      headers={'Location': scm_url})

        url, dest_filename = get_scm_archive_from_build_id(base_api_url, build_id)

        assert url == scm_url
        assert dest_filename == filename

    def test_connection_failure(self):
        base_api_url = 'https://example.com/{}/scm_archive'
        build_id = '1234'
        session = get_retrying_requests_session()
        (flexmock(session)
         .should_receive('get')
         .and_raise(requests.exceptions.RetryError))
        with pytest.raises(requests.exceptions.RetryError):
            get_scm_archive_from_build_id(base_api_url, build_id, session=session)
