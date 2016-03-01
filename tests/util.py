# -*- coding: utf-8 -*-
"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import pytest
import requests

from six import string_types


def is_string_type(obj):
    """
    Test whether obj is a string type

    :param obj: object to test
    :return: bool, whether obj is a string type
    """

    return any(isinstance(obj, strtype)
               for strtype in string_types)

def has_connection():
    try:
        requests.get("https://github.com/")
        return True
    except requests.ConnectionError:
        return False

# In case we run tests in an environment without internet connection.
requires_internet = pytest.mark.skipif(not has_connection(), reason="requires internet connection")
