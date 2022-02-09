"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import os
import paramiko
import pytest
import re
from flexmock import flexmock, Mock
from typing import Tuple

from atomic_reactor.utils.resource_manager import (RemoteHost,
                                                   SSH_COMMAND_TIMEOUT,
                                                   SLOTS_RELATIVE_PATH)


def get_mocked_cmd_result(
    stdout: str = "",
    stderr: str = "",
    code: int = 0
) -> Tuple[None, Mock, Mock]:

    chan = flexmock()
    chan.should_receive("recv_exit_status").and_return(code)

    out = flexmock(channel=chan)
    out.should_receive("read.decode.strip").and_return(stdout)

    err = flexmock()
    err.should_receive("read.decode.strip").and_return(stderr)
    return None, out, err


@pytest.mark.parametrize(("mkdir_stderr", "mkdir_code", "expected_result"), (
    ("", 0, True),
    ("mkdir: cannot create directory: ... permission denied", 1, False),
))
def test_host_is_operational(mkdir_stderr, mkdir_code, expected_result):
    flexmock(paramiko.SSHClient).should_receive("connect").and_return(None).at_least().times(1)

    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3)

    def mocked_command(cmd, timeout):
        assert timeout == SSH_COMMAND_TIMEOUT
        home = "/home/builder"
        if cmd == "pwd":
            return get_mocked_cmd_result(home)

        slots_dir = os.path.join(home, SLOTS_RELATIVE_PATH)
        if cmd == f"mkdir -p {slots_dir}":
            return get_mocked_cmd_result(stderr=mkdir_stderr, code=mkdir_code)

        assert False, f"Unexpect command: {cmd}"

    (
        flexmock(paramiko.SSHClient)
        .should_receive("exec_command")
        .times(2)
        .replace_with(mocked_command)
    )

    assert host.is_operational is expected_result


def test_check_slot_is_free_with_invalid_id(caplog):
    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3)
    # This is to avoid mocking the `pwd` command
    host._slots_dir = os.path.join("/home/builder", SLOTS_RELATIVE_PATH)
    free = host.is_free(3)
    assert "remote-host-001: invalid slot id 3, should be in" in caplog.text
    assert not free


@pytest.mark.parametrize(("testcmd_stdout", "testcmd_code", "expected_result"), (
    ("EMPTY", 0, True),
    ("", 0, False),
    ("", 1, False),
))
def test_check_slot_is_free(testcmd_stdout, testcmd_code, expected_result, caplog):
    flexmock(paramiko.SSHClient).should_receive("connect").and_return(None).at_least().times(1)

    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3)
    # This is to avoid mocking the `pwd` command
    host._slots_dir = os.path.join("/home/builder", SLOTS_RELATIVE_PATH)

    def mocked_command(cmd, timeout):
        assert timeout == SSH_COMMAND_TIMEOUT
        # Not perfect regex, but should be good enough
        testsize_cmd = re.compile("flock.*test.*echo EMPTY")
        if testsize_cmd.match(cmd):
            return get_mocked_cmd_result(stdout=testcmd_stdout, code=testcmd_code)

        assert False, f"Unexpect command: {cmd}"

    (
        flexmock(paramiko.SSHClient)
        .should_receive("exec_command")
        .times(1)
        .replace_with(mocked_command)
    )
    free = host.is_free(0)
    assert free is expected_result


def test_lock_an_invalid_slot_id(caplog):
    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3)
    # Slot id starts from 0
    locked = host.lock(3, "pr-123")
    assert "remote-host-001: invalid slot id 3, should be in" in caplog.text
    assert not locked


def test_lock_a_nonempty_slot():
    flexmock(paramiko.SSHClient).should_receive("connect").and_return(None).at_least().times(1)

    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3)
    # This is to avoid mocking the `pwd` command
    host._slots_dir = os.path.join("/home/builder", SLOTS_RELATIVE_PATH)

    def mocked_command(cmd, timeout):
        assert timeout == SSH_COMMAND_TIMEOUT
        # Not perfect regex match, but should be good enough
        lock_cmd = re.compile("flock.*test.*NONEMPTY.*echo.*pr-123@.*slot.*")
        if lock_cmd.match(cmd):
            return get_mocked_cmd_result(stdout="NONEMPTY")

        assert False, f"Unexpected cmd: {cmd}"

    (
        flexmock(paramiko.SSHClient)
        .should_receive("exec_command")
        .times(1)
        .replace_with(mocked_command)
    )
    locked = host.lock(0, "pr-123")
    assert not locked


def test_lock_an_occupied_slot(caplog):
    flexmock(paramiko.SSHClient).should_receive("connect").and_return(None).at_least().times(1)

    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3)
    # This is to avoid mocking the `pwd` command
    host._slots_dir = os.path.join("/home/builder", SLOTS_RELATIVE_PATH)

    def mocked_command(cmd, timeout):
        assert timeout == SSH_COMMAND_TIMEOUT
        # Not perfect regex match, but should be good enough
        lock_123_cmd = re.compile("flock.*test.*NONEMPTY.*echo.*pr-123@.*slot.*")
        if lock_123_cmd.match(cmd):
            return get_mocked_cmd_result()

        lock_234_cmd = re.compile("flock.*test.*NONEMPTY.*echo.*pr-234@.*slot.*")
        if lock_234_cmd.match(cmd):
            return get_mocked_cmd_result(stdout="NONEMPTY")

        assert False, f"Unexpected cmd: {cmd}"

    (
        flexmock(paramiko.SSHClient)
        .should_receive("exec_command")
        .times(2)
        .replace_with(mocked_command)
    )
    locked = host.lock(0, "pr-123")
    assert locked
    locked = host.lock(0, "pr-234")
    assert "remote-host-001: slot 0 is not free" in caplog.text
    assert not locked
