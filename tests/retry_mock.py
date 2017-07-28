"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from flexmock import flexmock
from atomic_reactor import util


def mock_get_retry_session():

    def custom_retries(*args, **kwargs):
        kwargs['times'] = 0
        return retry_fnc(*args, **kwargs)

    retry_fnc = util.get_retrying_requests_session

    (flexmock(util)
        .should_receive('get_retrying_requests_session')
        .replace_with(custom_retries))
