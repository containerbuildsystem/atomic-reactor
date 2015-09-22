# -*- coding: utf-8 -*-
"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from six import string_types


def is_string_type(obj):
    """
    Test whether obj is a string type

    :param obj: object to test
    :return: bool, whether obj is a string type
    """

    return any(isinstance(obj, strtype)
               for strtype in string_types)
