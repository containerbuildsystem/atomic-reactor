"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import json
import subprocess
import time

import requests
from urllib3 import Retry

import pytest
import responses
from flexmock import flexmock

from atomic_reactor.constants import (HTTP_MAX_RETRIES,
                                      HTTP_REQUEST_TIMEOUT,
                                      SUBPROCESS_MAX_RETRIES,
                                      SUBPROCESS_BACKOFF_FACTOR)
from atomic_reactor.utils import retries


@pytest.mark.parametrize('timeout', [None, 0, 10])
def test_session_with_timeout(timeout):
    """
    Test that session sets default timeout if not specified
    """
    session = retries.SessionWithTimeout()

    test_url = 'http://test.net'

    def mocked_request(method, url, **kwargs):
        assert method == 'GET'
        assert url == test_url
        assert 'timeout' in kwargs
        expected_timeout = timeout if timeout is not None else HTTP_REQUEST_TIMEOUT
        assert kwargs['timeout'] == expected_timeout

    (flexmock(requests.Session)
     .should_receive('request')
     .replace_with(mocked_request))

    if timeout is not None:
        session.get(test_url, timeout=timeout)
    else:
        session.get(test_url)


@pytest.mark.parametrize('times', [None, 0, 5])
def test_get_retrying_requests_session(times):
    """
    Test that retries are set properly for http(s):// adapters

    Most arguments are simply passed to Retry.__init__, test only basic functionality
    """
    if times is not None:
        session = retries.get_retrying_requests_session(times=times)
    else:
        session = retries.get_retrying_requests_session()

    http = session.adapters['http://']
    https = session.adapters['https://']

    assert isinstance(http.max_retries, Retry)
    assert isinstance(https.max_retries, Retry)

    expected_total = times if times is not None else HTTP_MAX_RETRIES
    assert http.max_retries.total == expected_total
    assert https.max_retries.total == expected_total


@responses.activate
@pytest.mark.parametrize('http_code', [399, 400, 401, 500, 599])
def test_log_error_response(http_code, caplog):
    api_url = 'https://localhost/api/v1/foo'
    json_data = {'message': 'value error'}
    responses.add(responses.GET, api_url, json=json_data, status=http_code)

    session = retries.get_retrying_requests_session()
    session.get(api_url)

    content = json.dumps(json_data).encode()
    expected = f"Error response from {api_url}: {content}"
    if 400 <= http_code <= 599:
        assert expected in caplog.text
    else:
        assert expected not in caplog.text


@pytest.mark.parametrize('retries_needed', [0, 1, SUBPROCESS_MAX_RETRIES])
def test_run_cmd_success(retries_needed, caplog):
    cmd = ["skopeo", "copy", "docker://a", "docker://b"]
    n_tries = 0

    def mock_check_output(*args, **kwargs):
        nonlocal n_tries
        n_tries += 1
        if n_tries > retries_needed:
            return b'some output'
        raise subprocess.CalledProcessError(1, cmd, output=b'something went wrong')

    (
        flexmock(subprocess)
        .should_receive('check_output')
        .with_args(cmd, stderr=subprocess.STDOUT)
        .times(retries_needed + 1)
        .replace_with(mock_check_output)
    )
    flexmock(time).should_receive('sleep').times(retries_needed)

    assert retries.run_cmd(cmd) == b'some output'

    assert caplog.text.count('Running skopeo copy docker://a docker://b') == retries_needed + 1
    assert caplog.text.count('skopeo failed with:\nsomething went wrong') == retries_needed

    for n in range(retries_needed):
        wait = SUBPROCESS_BACKOFF_FACTOR * 2 ** n
        assert f'Backing off run_cmd(...) for {wait:.1f}s' in caplog.text


def test_run_cmd_failure(caplog):
    cmd = ["skopeo", "copy", "docker://a", "docker://b"]
    total_tries = SUBPROCESS_MAX_RETRIES + 1

    (
        flexmock(subprocess)
        .should_receive('check_output')
        .with_args(cmd, stderr=subprocess.STDOUT)
        .times(total_tries)
        .and_raise(subprocess.CalledProcessError(1, cmd, output=b'something went wrong'))
    )
    flexmock(time).should_receive('sleep').times(SUBPROCESS_MAX_RETRIES)

    with pytest.raises(subprocess.CalledProcessError):
        retries.run_cmd(cmd)

    assert caplog.text.count('Running skopeo copy docker://a docker://b') == total_tries
    assert caplog.text.count('skopeo failed with:\nsomething went wrong') == total_tries

    for n in range(SUBPROCESS_MAX_RETRIES):
        wait = SUBPROCESS_BACKOFF_FACTOR * 2 ** n
        assert f'Backing off run_cmd(...) for {wait:.1f}s' in caplog.text
    assert f'Giving up run_cmd(...) after {total_tries} tries' in caplog.text
