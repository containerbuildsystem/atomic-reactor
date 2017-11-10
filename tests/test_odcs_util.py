"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.odcs_util import ODCSClient, OIDC_TOKEN_HEADER
from tests.retry_mock import mock_get_retry_session

import pytest
import responses
import six
import json


MODULE_NAME = 'eog'
MODULE_STREAM = 'f26'
MODULE_VERSION = "20170629213428"
MODULE_NSV = '-'.join([MODULE_NAME, MODULE_STREAM, MODULE_VERSION])

ODCS_URL = 'https://odcs.fedoraproject.org/odcs/1'

COMPOSE_ID = 84


def compose_json(state, state_name, source_type=2, source=MODULE_NSV):
    return json.dumps({
        'flags': [],
        'id': COMPOSE_ID,
        'owner': 'Unknown',
        'result_repo': 'http://odcs.fedoraproject.org/composes/latest-odcs-84-1/compose/Temporary',
        'source': source,
        'source_type': source_type,
        'state': state,
        'state_name': state_name
    })


@responses.activate
def test_create_compose(tmpdir):
    odcs_token = 'green_eggs_and_ham'
    secrets_path = tmpdir.mkdir('secret')
    secrets_path.join('token').write(odcs_token)

    mock_get_retry_session()

    def handle_composes_post(request):
        assert request.headers[OIDC_TOKEN_HEADER] == odcs_token

        if isinstance(request.body, six.text_type):
            body = request.body
        else:
            body = request.body.decode()
        body_json = json.loads(body)
        assert body_json['source']['type'] == 'module'
        assert body_json['source']['source'] == MODULE_NSV
        return (200, {}, compose_json(0, 'wait'))

    responses.add_callback(responses.POST, ODCS_URL + '/composes/',
                           content_type='application/json',
                           callback=handle_composes_post)

    odcs_client = ODCSClient(ODCS_URL, token=odcs_token)
    odcs_client.start_compose(source_type='module', source=MODULE_NSV)


@responses.activate
def test_wait_for_compose(tmpdir):
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
            response_json = compose_json(2, 'done')
        state['count'] += 1

        return (200, {}, response_json)

    responses.add_callback(responses.GET, ODCS_URL + '/composes/%d' % COMPOSE_ID,
                           content_type='application/json',
                           callback=handle_composes_get)

    odcs_client = ODCSClient(ODCS_URL, token=odcs_token)
    compose_info = odcs_client.wait_for_compose(COMPOSE_ID)
