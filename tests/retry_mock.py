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
    (flexmock(util)
        .should_receive('_http_retries_disabled')
        .and_return(True))
