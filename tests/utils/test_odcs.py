"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.utils.odcs import (ODCSClient, MULTILIB_METHOD_DEFAULT,
                                       WaitComposeToFinishTimeout)
from tests.retry_mock import mock_get_retry_session

import flexmock
import pytest
import responses
import json
import time


MODULE_NAME = 'eog'
MODULE_STREAM = 'f26'
MODULE_VERSION = "20170629213428"
MODULE_NSV = '-'.join([MODULE_NAME, MODULE_STREAM, MODULE_VERSION])

ODCS_URL = 'https://odcs.fedoraproject.org/odcs/1/'

COMPOSE_ID = 84

SOURCE_TYPE_ENUM = {
    'tag': 1,
    'module': 2,
}


@pytest.fixture(params=(
    (False, None, None),
    (False, 'green_eggs_and_ham', None),
    (True, 'green_eggs_and_ham', None),
    (False, None, 'spam_cert'),
    (True, None, 'spam_cert'),
))
def odcs_client(tmpdir, request):
    insecure, token, cert = request.param

    mock_get_retry_session()

    odcs_client = ODCSClient(ODCS_URL, insecure=insecure, token=token, cert=cert)

    assert odcs_client.session.verify == (not insecure)
    assert odcs_client.session.cert == cert

    if token:
        expected_token_header = 'Bearer {}'.format(token)
        token_header = odcs_client.session.headers[ODCSClient.OIDC_TOKEN_HEADER]
        assert token_header == expected_token_header
    else:
        assert ODCSClient.OIDC_TOKEN_HEADER not in odcs_client.session.headers

    return odcs_client


def compose_json(state, state_name, source_type='module', source=MODULE_NSV,
                 compose_id=COMPOSE_ID, state_reason=None):
    compose = {
        'flags': [],
        'id': compose_id,
        'owner': 'Unknown',
        'result_repo': 'http://odcs.fedoraproject.org/composes/latest-odcs-84-1/compose/Temporary',
        'source': source,
        'source_type': SOURCE_TYPE_ENUM[source_type],
        'state': state,
        'state_name': state_name
    }
    if state_reason:
        compose['state_reason'] = state_reason
    return json.dumps(compose)


@responses.activate
@pytest.mark.parametrize('arches', (
    None,
    ['x86_64'],
    ['breakfast', 'lunch'],
))
@pytest.mark.parametrize(('source', 'source_type', 'packages', 'expected_packages', 'sigkeys',
                          'modular_koji_tags', 'expected_koji_tags'), (
    (MODULE_NSV, 'module', None, None, None, [], None),
    (MODULE_NSV, 'module', None, None, None, ['release'], ['release']),
    ('my-tag', 'tag', None, None, None, ['release'], ['release']),
    ('my-tag', 'tag', ['spam', 'bacon', 'eggs'], ['spam', 'bacon', 'eggs'], None, None, None),
    ('my-tag', 'tag', ['spam', 'bacon', 'eggs'], ['spam', 'bacon', 'eggs'], [], None, None),
    ('my-tag', 'tag', ['spam', 'bacon', 'eggs'], ['spam', 'bacon', 'eggs'], "", None, None),
    ('my-tag', 'tag', ['spam', 'bacon', 'eggs'], ['spam', 'bacon', 'eggs'], ['B456', 'R123'],
     None, None),
    ('my-tag', 'tag', ['spam', 'bacon', 'eggs'], None, [], ['release'], ['release']),
    ('my-tag', 'tag', None, None, [], ['release'], ['release']),
    ('my-tag', 'tag', None, None, ['B456', 'R123'], ['release'], ['release']),
))
@pytest.mark.parametrize('flags', (
    None,
    ['no_deps'],
    ['breakfast', 'lunch'],
))
@pytest.mark.parametrize(('multilib_arches', 'multilib_method', 'expected_method'), (
    (None, None, None),
    (None, ['all'], None),
    (['x86_64'], ['all'], ['all']),
    (['breakfast', 'lunch'], ['some', 'random'], ['some', 'random']),
    (['breakfast', 'lunch'], [], MULTILIB_METHOD_DEFAULT)
))
def test_create_compose(odcs_client, source, source_type, packages, expected_packages, sigkeys,
                        modular_koji_tags, expected_koji_tags,
                        arches, flags, multilib_arches, multilib_method, expected_method):

    def handle_composes_post(request):
        assert_request_token(request, odcs_client.session)

        if isinstance(request.body, str):
            body = request.body
        else:
            body = request.body.decode()
        body_json = json.loads(body)

        assert body_json['source']['type'] == source_type
        assert body_json['source']['source'] == source
        assert body_json['source'].get('packages') == expected_packages
        assert body_json['source'].get('sigkeys') == sigkeys
        assert body_json['source'].get('modular_koji_tags') == expected_koji_tags
        assert body_json.get('flags') == flags
        assert body_json.get('arches') == arches
        assert body_json.get('multilib_arches') == multilib_arches
        if expected_method is not None:
            assert sorted(body_json.get('multilib_method')) == sorted(expected_method)
        else:
            assert 'multilib_method' not in body_json
        return (200, {}, compose_json(0, 'wait', source_type=source_type, source=source))

    responses.add_callback(responses.POST, '{}composes/'.format(ODCS_URL),
                           content_type='application/json',
                           callback=handle_composes_post)

    odcs_client.start_compose(source_type=source_type, source=source, packages=packages,
                              sigkeys=sigkeys, arches=arches, flags=flags,
                              multilib_arches=multilib_arches, multilib_method=multilib_method,
                              modular_koji_tags=modular_koji_tags)


@responses.activate
@pytest.mark.parametrize(('final_state_id', 'final_state_name', 'expect_exc', 'state_reason'), (
    (2, 'done', False, None,),
    (4, 'failed', 'Failed request for compose_id={}: Unknown'.format(COMPOSE_ID), None),
    (4, 'failed', 'Failed request for compose_id={}: Uh oh!'.format(COMPOSE_ID), 'Uh oh!'),
))
def test_wait_for_compose(odcs_client, final_state_id, final_state_name, expect_exc, state_reason):
    state = {'count': 1}

    def handle_composes_get(request):
        assert_request_token(request, odcs_client.session)

        if state['count'] == 1:
            response_json = compose_json(1, 'generating')
        else:
            response_json = compose_json(final_state_id, final_state_name,
                                         state_reason=state_reason)
        state['count'] += 1

        return (200, {}, response_json)

    responses.add_callback(responses.GET, '{}composes/{}'.format(ODCS_URL, COMPOSE_ID),
                           content_type='application/json',
                           callback=handle_composes_get)

    (flexmock(time)
        .should_receive('sleep')
        .and_return(None))

    if expect_exc:
        with pytest.raises(RuntimeError) as exc_info:
            odcs_client.wait_for_compose(COMPOSE_ID)
        assert expect_exc in str(exc_info.value)
    else:
        odcs_client.wait_for_compose(COMPOSE_ID)


@responses.activate
def test_renew_compose(odcs_client):
    new_compose_id = COMPOSE_ID + 1

    def handle_composes_patch(request):
        assert_request_token(request, odcs_client.session)
        return (200, {}, compose_json(0, 'generating', compose_id=new_compose_id))

    responses.add_callback(responses.PATCH, '{}composes/{}'.format(ODCS_URL, COMPOSE_ID),
                           content_type='application/json',
                           callback=handle_composes_patch)

    odcs_client.renew_compose(COMPOSE_ID)
    odcs_client.renew_compose(COMPOSE_ID, ['SIGKEY1', 'SIGKEY2'])


def assert_request_token(request, session):
    expected_token = None
    if ODCSClient.OIDC_TOKEN_HEADER in session.headers:
        expected_token = session.headers[ODCSClient.OIDC_TOKEN_HEADER]
    assert request.headers.get(ODCSClient.OIDC_TOKEN_HEADER) == expected_token


@responses.activate
@pytest.mark.parametrize('timeout', (0, 20))
def test_wait_for_compose_timeout(timeout):
    compose_url = '{}composes/{}'.format(ODCS_URL, COMPOSE_ID)

    responses.add(responses.GET, compose_url, body=compose_json(0, 'generating'))
    (flexmock(time)
        .should_receive('time')
        .and_return(2000, 1000)  # end time, start time
        .one_by_one())

    odcs_client = ODCSClient(ODCS_URL, timeout=timeout)
    expected_error = r'Timeout of waiting for compose \d+'
    with pytest.raises(WaitComposeToFinishTimeout, match=expected_error):
        odcs_client.wait_for_compose(COMPOSE_ID)
