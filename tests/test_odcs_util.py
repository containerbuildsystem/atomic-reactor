"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.odcs_util import ODCSClient
from tests.retry_mock import mock_get_retry_session

import pytest
import responses
import six
import json


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


def compose_json(state, state_name, source_type='module', source=MODULE_NSV,
                 compose_id=COMPOSE_ID):
    return json.dumps({
        'flags': [],
        'id': compose_id,
        'owner': 'Unknown',
        'result_repo': 'http://odcs.fedoraproject.org/composes/latest-odcs-84-1/compose/Temporary',
        'source': source,
        'source_type': SOURCE_TYPE_ENUM[source_type],
        'state': state,
        'state_name': state_name
    })


@responses.activate
@pytest.mark.parametrize(('source', 'source_type', 'packages', 'sigkeys'), (
    (MODULE_NSV, 'module', None, None),
    ('my-tag', 'tag', ['spam', 'bacon', 'eggs'], None),
    ('my-tag', 'tag', ['spam', 'bacon', 'eggs'], ['B456', 'R123']),
    ('my-tag', 'tag', ['spam', 'bacon', 'eggs'], ""),
    ('my-tag', 'tag', ['spam', 'bacon', 'eggs'], []),
))
def test_create_compose(tmpdir, source, source_type, packages, sigkeys):
    odcs_token = 'green_eggs_and_ham'
    secrets_path = tmpdir.mkdir('secret')
    secrets_path.join('token').write(odcs_token)

    mock_get_retry_session()

    def handle_composes_post(request):
        assert request.headers[ODCSClient.OIDC_TOKEN_HEADER] == odcs_token

        if isinstance(request.body, six.text_type):
            body = request.body
        else:
            body = request.body.decode()
        body_json = json.loads(body)
        assert body_json['source']['type'] == source_type
        assert body_json['source']['source'] == source
        assert body_json['source'].get('packages') == packages
        assert body_json['source'].get('sigkeys') == sigkeys
        return (200, {}, compose_json(0, 'wait', source_type=source_type, source=source))

    responses.add_callback(responses.POST, '{}composes/'.format(ODCS_URL),
                           content_type='application/json',
                           callback=handle_composes_post)

    odcs_client = ODCSClient(ODCS_URL, token=odcs_token)
    odcs_client.start_compose(source_type=source_type, source=source, packages=packages,
                              sigkeys=sigkeys)


@responses.activate
@pytest.mark.parametrize(('final_state_id', 'final_state_name', 'expect_exc'), (
    (2, 'done', False),
    (4, 'failed', True),
))
def test_wait_for_compose(tmpdir, final_state_id, final_state_name, expect_exc):
    odcs_token = 'green_eggs_and_ham'
    secrets_path = tmpdir.mkdir('secret')
    secrets_path.join('token').write(odcs_token)

    mock_get_retry_session()

    state = {'count': 1}

    def handle_composes_get(request):
        assert request.headers['OIDC_access_token'] == 'green_eggs_and_ham'

        if state['count'] == 1:
            response_json = compose_json(1, 'generating')
        else:
            response_json = compose_json(final_state_id, final_state_name)
        state['count'] += 1

        return (200, {}, response_json)

    responses.add_callback(responses.GET, '{}composes/{}'.format(ODCS_URL, COMPOSE_ID),
                           content_type='application/json',
                           callback=handle_composes_get)

    odcs_client = ODCSClient(ODCS_URL, token=odcs_token)
    if expect_exc:
        with pytest.raises(RuntimeError) as exc_info:
            odcs_client.wait_for_compose(COMPOSE_ID)
        assert 'Failed request' in str(exc_info.value)
    else:
        odcs_client.wait_for_compose(COMPOSE_ID)


@responses.activate
def test_renew_compose(tmpdir):
    odcs_token = 'green_eggs_and_ham'
    secrets_path = tmpdir.mkdir('secret')
    secrets_path.join('token').write(odcs_token)

    mock_get_retry_session()

    new_compose_id = COMPOSE_ID + 1

    def handle_composes_patch(request):
        assert request.headers[ODCSClient.OIDC_TOKEN_HEADER] == odcs_token
        return (200, {}, compose_json(0, 'generating', compose_id=new_compose_id))

    responses.add_callback(responses.PATCH, '{}composes/{}'.format(ODCS_URL, COMPOSE_ID),
                           content_type='application/json',
                           callback=handle_composes_patch)

    odcs_client = ODCSClient(ODCS_URL, token=odcs_token)
    odcs_client.renew_compose(COMPOSE_ID)
