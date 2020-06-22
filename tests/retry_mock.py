"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

from flexmock import flexmock
import atomic_reactor.utils.retries


def mock_get_retry_session():
    (flexmock(atomic_reactor.utils.retries)
        .should_receive('_http_retries_disabled')
        .and_return(True))
