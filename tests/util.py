# -*- coding: utf-8 -*-
"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import pytest
import requests
import uuid

from six import string_types


def uuid_value():
    return uuid.uuid4().hex


def is_string_type(obj):
    """
    Test whether obj is a string type

    :param obj: object to test
    :return: bool, whether obj is a string type
    """

    return any(isinstance(obj, strtype)
               for strtype in string_types)


class InternetConnectionChecker(object):
    """Indicate whether there is a usable Internet connection"""

    def __init__(self):
        self._has_conn = None

    def __bool__(self):
        if self._has_conn is None:
            try:
                requests.get("http://github.com/", allow_redirects=False, timeout=5)
                self._has_conn = True
            except requests.ConnectionError:
                self._has_conn = False
        return self._has_conn

has_connection = InternetConnectionChecker()


# In case we run tests in an environment without internet connection.
requires_internet = pytest.mark.skipif(not has_connection, reason="requires internet connection")
