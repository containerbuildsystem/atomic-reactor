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
from atomic_reactor.utils.pnc import PNCUtil

PNC_BASE_API_URL = 'http://pnc.localhost/pnc-rest/v2'
PNC_GET_SCM_ARCHIVE_PATH = 'builds/{}/scm-archive'


def mock_pnc_map():
    return {'base_api_url': PNC_BASE_API_URL,
            'get_scm_archive_path': PNC_GET_SCM_ARCHIVE_PATH}


@pytest.mark.usefixtures('user_params')
class TestGetSCMArchiveFromBuildID(object):

    @responses.activate
    def test_connection_filename_in_header(self):
        build_id = '1234'
        filename = 'source.tar.gz'
        scm_url = f'https://code.example.com/{filename};sf=tgz'
        content = b'abc'
        reader = BufferedReader(BytesIO(content), buffer_size=1)
        # to mock this URL we have to construct it manually first
        get_scm_archive_request_url = PNC_BASE_API_URL + '/' + PNC_GET_SCM_ARCHIVE_PATH

        responses.add(responses.GET, get_scm_archive_request_url.format(build_id), body=reader,
                      status=302, headers={'Location': scm_url})
        responses.add(responses.HEAD, scm_url, body='', status=200,
                      headers={'Content-disposition': f'filename="{filename}"'})

        pnc_util = PNCUtil(mock_pnc_map())

        url, dest_filename = pnc_util.get_scm_archive_from_build_id(build_id)

        assert url == scm_url
        assert dest_filename == filename

    @responses.activate
    def test_connection_filename_in_url(self):
        build_id = '1234'
        filename = 'source.tar.gz'
        scm_url = f'https://code.example.com/{filename}'
        # to mock this URL we have to construct it manually first
        get_scm_archive_request_url = PNC_BASE_API_URL + '/' + PNC_GET_SCM_ARCHIVE_PATH

        responses.add(responses.GET, get_scm_archive_request_url.format(build_id), body='',
                      status=302, headers={'Location': scm_url})

        pnc_util = PNCUtil(mock_pnc_map())

        url, dest_filename = pnc_util.get_scm_archive_from_build_id(build_id)

        assert url == scm_url
        assert dest_filename == filename

    def test_connection_failure(self):
        build_id = '1234'
        session = get_retrying_requests_session()

        (flexmock(session)
         .should_receive('get')
         .and_raise(requests.exceptions.RetryError))

        pnc_util = PNCUtil(mock_pnc_map(), session)

        with pytest.raises(requests.exceptions.RetryError):
            pnc_util.get_scm_archive_from_build_id(build_id)
