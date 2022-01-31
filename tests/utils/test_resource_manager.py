"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from atomic_reactor.utils.resource_manager import Host, ResourceManager
import pytest


def test_host_init():
    my_host = Host('user', 'hostname')
    assert my_host.username = 'user'
    assert my_host.hostname = 'hostname'
