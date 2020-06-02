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

from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)

from six import string_types


def add_koji_map_in_workflow(workflow, hub_url, root_url=None, reserve_build=None,
                             delegate_task=None, delegated_priority=None,
                             proxyuser=None, ssl_certs_dir=None,
                             krb_principal=None, krb_keytab=None):
    config_key = workflow.plugin_workspace.get(ReactorConfigPlugin.key)
    if not config_key:
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

    reactor_config = workflow.plugin_workspace[ReactorConfigPlugin.key].get(WORKSPACE_CONF_KEY)
    if not reactor_config:
        reactor_config = ReactorConfig({})

    koji_map = reactor_config.conf['koji'] = {
        'hub_url': hub_url,
        'auth': {},
    }

    if root_url is not None:
        koji_map['root_url'] = root_url

    if reserve_build is not None:
        koji_map['reserve_build'] = reserve_build

    if delegate_task is not None:
        koji_map['delegate_task'] = delegate_task

    if delegated_priority:
        koji_map['delegated_task_priority'] = delegated_priority

    if proxyuser:
        koji_map['auth']['proxyuser'] = proxyuser

    if ssl_certs_dir:
        koji_map['auth']['ssl_certs_dir'] = ssl_certs_dir

    if krb_principal:
        koji_map['auth']['krb_principal'] = str(krb_principal)

    if krb_keytab:
        koji_map['auth']['krb_keytab_path'] = str(krb_keytab)

    workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] = reactor_config


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


def has_connection():
    try:
        requests.head("http://github.com/")
        return True
    except requests.ConnectionError:
        return False


# In case we run tests in an environment without internet connection.
requires_internet = pytest.mark.skipif(not has_connection(), reason="requires internet connection")
