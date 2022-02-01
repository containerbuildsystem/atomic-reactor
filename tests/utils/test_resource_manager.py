"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import pytest
from unittest import mock
from atomic_reactor.utils.resource_manager import Host, ResourceManager


def test_host_init():
    my_host = Host('user', 'hostname')
    assert my_host.username == 'user'
    assert my_host.hostname == 'hostname'
    assert my_host.slots == []
    assert my_host.free_slots == []


def test_host_read_info_data():
    HOST_INFO = '{"hostname": "tst", "arch": "x86", "max_slot_count": 0}'
    with mock.patch.object(Host, '_ssh_run_remote_command', HOST_INFO) as mock_obj:
        my_host = Host('user', 'hostname')
        assert my_host.check_avail() == []
