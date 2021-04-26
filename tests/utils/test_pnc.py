"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import json
from io import BufferedReader, BytesIO

import pytest
import requests
import responses
from flexmock import flexmock

from atomic_reactor.util import get_retrying_requests_session
from atomic_reactor.utils.pnc import PNCUtil

PNC_BASE_API_URL = 'http://pnc.localhost/pnc-rest/v2'
PNC_GET_SCM_ARCHIVE_PATH = 'builds/{}/scm-archive'
PNC_GET_ARTIFACT_PATH = 'artifacts/{}'


def mock_pnc_map():
    return {
        'base_api_url': PNC_BASE_API_URL,
        'get_scm_archive_path': PNC_GET_SCM_ARCHIVE_PATH,
        'get_artifact_path': PNC_GET_ARTIFACT_PATH,
    }


@pytest.mark.usefixtures('user_params')
class TestPNCUtil(object):

    @responses.activate
    def test_get_artifact(self):
        artifact_id = '12345'
        public_url = 'https://code.example.com/artifact.jar'
        artifact_response = {
            'id': artifact_id,
            'publicUrl': public_url,
            'md5': 'abcd',
            'sha1': 'abcd',
            'sha256': 'abcd',
        }
        pnc_util = PNCUtil(mock_pnc_map())

        # to mock this URL we have to construct it manually first
        get_artifact_request_url = PNC_BASE_API_URL + '/' + PNC_GET_ARTIFACT_PATH

        responses.add(responses.GET, get_artifact_request_url.format(artifact_id),
                      body=json.dumps(artifact_response), status=200)
        responses.add(responses.HEAD, public_url, body='abc', status=200)

        url, checksums = pnc_util.get_artifact(artifact_id)

        assert public_url == url
        assert checksums == {'md5': 'abcd', 'sha1': 'abcd', 'sha256': 'abcd'}

    @responses.activate
    def test_get_scm_archive_filename_in_header(self):
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
    def test_get_scm_archive_filename_in_url(self):
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
        artifact_id = '12345'
        session = get_retrying_requests_session()

        (flexmock(session)
         .should_receive('get')
         .and_raise(requests.exceptions.RetryError))

        pnc_util = PNCUtil(mock_pnc_map(), session)

        with pytest.raises(requests.exceptions.RetryError):
            pnc_util.get_scm_archive_from_build_id(build_id)

        with pytest.raises(requests.exceptions.RetryError):
            pnc_util.get_artifact(artifact_id)
