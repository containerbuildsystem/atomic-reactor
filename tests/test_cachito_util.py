"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import

from atomic_reactor.cachito_util import (
    CachitoAPI, CachitoAPIInvalidRequest, CachitoAPIRequestTimeout, CachitoAPIUnsuccessfulRequest)

from requests.exceptions import HTTPError
import pytest
import responses
import json
import os.path


CACHITO_URL = 'http://cachito.example.com'
CACHITO_REQUEST_ID = 123
CACHITO_REQUEST_DOWNLOAD_URL = \
    '{}/api/v1/requests/{}/download'.format(CACHITO_URL, CACHITO_REQUEST_ID)
CACHITO_REQUEST_REF = 'e1be527f39ec31323f0454f7d1422c6260b00580'
CACHITO_REQUEST_REPO = 'https://github.com/release-engineering/retrodep.git'


@responses.activate
@pytest.mark.parametrize('additional_params', (
    {},
    {'flags': ['spam', 'bacon']},
    {'pkg_managers': ['gomod']},
    {'user': 'ham'},
))
def test_request_sources(additional_params):

    def handle_request_sources(http_request):
        body_json = json.loads(http_request.body)

        assert body_json['repo'] == CACHITO_REQUEST_REPO
        assert body_json['ref'] == CACHITO_REQUEST_REF
        for key, value in additional_params.items():
            assert body_json[key] == value

        return (201, {}, json.dumps({'id': CACHITO_REQUEST_ID}))

    responses.add_callback(
        responses.POST,
        '{}/api/v1/requests'.format(CACHITO_URL),
        content_type='application/json',
        callback=handle_request_sources)

    api = CachitoAPI(CACHITO_URL)
    response = api.request_sources(CACHITO_REQUEST_REPO, CACHITO_REQUEST_REF, **additional_params)
    assert response['id'] == CACHITO_REQUEST_ID


@responses.activate
@pytest.mark.parametrize(('status_code', 'error', 'error_body'), (
    (400, CachitoAPIInvalidRequest, json.dumps({'error': 'read the docs, please'})),
    (500, HTTPError, 'Internal Server Error'),
))
def test_request_sources_error(status_code, error, error_body):
    responses.add(
        responses.POST,
        '{}/api/v1/requests'.format(CACHITO_URL),
        content_type='application/json',
        body=error_body,
        status=status_code,
    )

    with pytest.raises(error):
        CachitoAPI(CACHITO_URL).request_sources(CACHITO_REQUEST_REPO, CACHITO_REQUEST_REF)


@responses.activate
@pytest.mark.parametrize('burst_params', (
    {'burst_retry': 0.01, 'burst_length': 0.5, 'slow_retry': 0.2},
    # Set the burst_length to lower than burst_retry to trigger the slow_retry :)
    {'burst_retry': 0.01, 'burst_length': 0.001, 'slow_retry': 0.01},
))
@pytest.mark.parametrize('cachito_request', (
    CACHITO_REQUEST_ID,
    {'id': CACHITO_REQUEST_ID},
))
def test_wait_for_request(burst_params, cachito_request):
    states = ['in_progress', 'in_progress', 'complete']
    expected_total_responses_calls = len(states)
    expected_final_state = states[-1]

    def handle_wait_for_request(http_request):
        state = states.pop(0)
        return (200, {}, json.dumps({'id': CACHITO_REQUEST_ID, 'state': state}))

    responses.add_callback(
        responses.GET,
        '{}/api/v1/requests/{}'.format(CACHITO_URL, CACHITO_REQUEST_ID),
        content_type='application/json',
        callback=handle_wait_for_request)

    response = CachitoAPI(CACHITO_URL).wait_for_request(cachito_request, **burst_params)
    assert response['id'] == CACHITO_REQUEST_ID
    assert response['state'] == expected_final_state
    assert len(responses.calls) == expected_total_responses_calls


@responses.activate
def test_wait_for_request_timeout():
    responses.add(
        responses.GET,
        '{}/api/v1/requests/{}'.format(CACHITO_URL, CACHITO_REQUEST_ID),
        content_type='application/json',
        status=200,
        body=json.dumps({'id': CACHITO_REQUEST_ID, 'state': 'in_progress'}),
    )

    # Hit the timeout during bursting to make the test faster
    burst_params = {'burst_retry': 0.001, 'burst_length': 0.02, 'timeout': 0.01}
    with pytest.raises(CachitoAPIRequestTimeout):
        CachitoAPI(CACHITO_URL).wait_for_request(CACHITO_REQUEST_ID, **burst_params)


@responses.activate
@pytest.mark.parametrize('error_state', ('failed', 'stale'))
def test_wait_for_unsuccessful_request(error_state):
    states = ['in_progress', 'in_progress', error_state]
    expected_total_responses_calls = len(states)

    def handle_wait_for_request(http_request):
        state = states.pop(0)
        return (200, {}, json.dumps({'id': CACHITO_REQUEST_ID, 'state': state}))

    responses.add_callback(
        responses.GET,
        '{}/api/v1/requests/{}'.format(CACHITO_URL, CACHITO_REQUEST_ID),
        content_type='application/json',
        callback=handle_wait_for_request)

    burst_params = {'burst_retry': 0.001, 'burst_length': 0.5}
    with pytest.raises(CachitoAPIUnsuccessfulRequest):
        CachitoAPI(CACHITO_URL).wait_for_request(CACHITO_REQUEST_ID, **burst_params)
    assert len(responses.calls) == expected_total_responses_calls


def test_wait_for_request_bad_request_type():
    with pytest.raises(ValueError, match=r'Unexpected request type'):
        CachitoAPI(CACHITO_URL).wait_for_request('spam')


@responses.activate
@pytest.mark.parametrize('cachito_request', (
    CACHITO_REQUEST_ID,
    {'id': CACHITO_REQUEST_ID},
))
def test_download_sources(tmpdir, cachito_request):
    blob = 'glop-glop-I\'m-a-blob'
    expected_dest_path = os.path.join(str(tmpdir), 'remote-sources.tar.gz')

    responses.add(
        responses.GET,
        '{}/api/v1/requests/{}/download'.format(CACHITO_URL, CACHITO_REQUEST_ID),
        body=blob)

    dest_path = CachitoAPI(CACHITO_URL).download_sources(cachito_request, str(tmpdir))

    assert dest_path == expected_dest_path
    with open(dest_path) as f:
        assert f.read() == blob


def test_download_sources_bad_request_type(tmpdir):
    with pytest.raises(ValueError, match=r'Unexpected request type'):
        CachitoAPI(CACHITO_URL).download_sources('spam', str(tmpdir))


@pytest.mark.parametrize('cachito_request', (
    CACHITO_REQUEST_ID,
    {'id': CACHITO_REQUEST_ID},
))
def test_assemble_download_url(tmpdir, cachito_request):
    url = CachitoAPI(CACHITO_URL).assemble_download_url(cachito_request)
    assert url == CACHITO_REQUEST_DOWNLOAD_URL
