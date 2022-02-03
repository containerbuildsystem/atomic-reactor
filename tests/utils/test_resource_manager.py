"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import pytest
from unittest import mock
from atomic_reactor.utils.resource_manager import Host, ResourceManager


class TestHost:
    def test_host_init():
        my_host = Host('user', 'hostname')
        assert my_host.username == 'user'
        assert my_host.hostname == 'hostname'
        assert my_host.slots == []
        assert my_host.free_slots == []

    def test_host_read_info_data():
        HOST_INFO = '{"hostname": "tst", "arch": "x86", "max_slot_count": 0}'
        mock.patch.object(Host, '_ssh_run_remote_command', HOST_INFO)
        my_host = Host('user', 'hostname')
        assert my_host.check_avail() == []

    def test_host_check_avail():
        HOST_INFO = '{"hostname": "tst", "arch": "x86", "max_slot_count": 1}'
        SLOT_INFO = '{"prid": "abc", "locked": "2022-02-03 08:33:33.404274"}'
        mock.patch.object(Host, '_read_info_data', HOST_INFO)
        mock.patch.object(Host, '_read_slot_data', SLOT_INFO)
        my_host = Host('user', 'hostname')
        assert my_host.check_avail() == [1]
        
    def test_host_lock():
        mock.patch.object(Host, '_ssh_run_remote_command',  b'0\n')
        my_host = Host('user', 'hostname')
        assert my_host.lock()

    def test_host_unlock():
        mock.patch.object(Host, '_ssh_run_remote_command',  '')
        my_host = Host('user', 'hostname')
        assert my_host.unlock()
