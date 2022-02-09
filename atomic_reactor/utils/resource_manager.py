"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import backoff
import logging
import os
import paramiko
from contextlib import contextmanager
from datetime import datetime
from shlex import quote
from typing import List, Optional

SSH_COMMAND_TIMEOUT = 45
TIMEOUT_FAIL_BUILD = 30
FLOCK_TIMEOUT = 10
FLOCK_CMD = f"flock -w {FLOCK_TIMEOUT}"
SLOTS_RELATIVE_PATH = "osbs_slots"
RETRY_ON_SSH_EXCEPTIONS = (paramiko.ssh_exception.NoValidConnectionsError,
                           paramiko.ssh_exception.SSHException)
BACKOFF_FACTOR = 5
MAX_RETRIES = 3

logger = logging.getLogger(__name__)


class RemoteHost:

    def __init__(self, *, hostname: str, username: str, ssh_keyfile: str, slots: int):
        """
        :param hostname: str, remote hostname for ssh connection
        :param username: str, username for ssh connection
        :param ssh_keyfile: str, filepath to ssh private key
        :param slots: int, number of max allowed slots on remote host
        """
        self._hostname = hostname
        self._username = username
        self._ssh_keyfile = ssh_keyfile
        self.slots = slots
        self._slots_dir = None

    @property
    def hostname(self):
        return self._hostname

    @property
    def username(self):
        return self._username

    @property
    def ssh_keyfile(self):
        return self._ssh_keyfile

    @property
    def slots_dir(self):
        if self._slots_dir is None:
            home, _, _ = self._run("pwd")
            self._slots_dir = os.path.join(home, SLOTS_RELATIVE_PATH)
        return self._slots_dir

    def _get_slot_path(self, slot_id: int) -> str:
        return os.path.join(self.slots_dir, f"slot_{slot_id}")

    def _is_valid_slot_id(self, slot_id: int) -> bool:
        valid_slots = range(self.slots)
        if slot_id not in valid_slots:
            logger.error("%s: invalid slot id %s, should be in: %s",
                         self.hostname, slot_id, list(valid_slots))
            return False
        return True

    @property
    def is_operational(self) -> bool:
        """ Check whether this host is operational """
        try:
            _, _, code = self._run(f"mkdir -p {self.slots_dir}")
        except Exception as e:
            logger.exception("%s: host is not operational: %s", self.hostname, e)
            return False
        if code != 0:
            logger.error("%s: cann't prepare slots directory", self.hostname)
            return False
        return True

    def is_free(self, slot_id: int) -> bool:
        """
        Check whether a slot is in free state

        :param slot_id: int, slot ID
        :return: True if slot is in free state, otherwise False
        """
        if not self._is_valid_slot_id(slot_id):
            return False

        slot_path = self._get_slot_path(slot_id)

        # If slot file is empty, it means slot is not occupied by any pipelinerun.
        # Check the echo output instead of just exit code to distinguish command
        # error and file size test failure
        try:
            stdout, _, _ = self._flock_run(slot_path, f"test -s {slot_path} || echo EMPTY")
        except Exception as e:
            logger.warning("%s: failed to get state of slot %s: %s", self.hostname, slot_id, e)
            return False

        return stdout == "EMPTY"

    def lock(self, slot_id: int, prid: str) -> bool:
        """
        Lock slot for a pipelinerun

        :param slot_id: int, slot ID
        :param prid: str, pipeline ID
        :return: True if slot is locked for pipelinerun successfully, otherwise False
        """
        if not self._is_valid_slot_id(slot_id):
            return False

        slot_path = self._get_slot_path(slot_id)
        try:
            data = f"{prid}@{datetime.utcnow().isoformat()}"
            logger.debug("%s: try to lock slot %s for pipelinerun %s",
                         self.hostname, slot_id, prid)
            # Only write lock content when slot file is empty, when slot file is not
            # empty, command output is "NONEMPTY"
            cmd = f"test -s {slot_path} && echo NONEMPTY || echo {data} > {slot_path}"
            stdout, stderr, code = self._flock_run(slot_path, cmd)
        except Exception as e:
            logger.info("%s: unable to lock slot %s for pipelinerun %s: %s",
                        self.hostname, slot_id, prid, e)
            return False

        if code != 0:
            logger.info("%s: unable to lock slot %s for pipelinerun %s: %s",
                        self.hostname, slot_id, prid, stderr)
            return False

        if stdout == "NONEMPTY":
            logger.error("%s: slot %s is not free", self.hostname, slot_id)
            return False

        logger.info("%s: slot %s is locked for pipelinerun %s", self.hostname, slot_id, prid)
        return True

    def unlock(self, slot_id: int, prid: str) -> bool:
        """
        Unlock slot for a pipelinerun

        :param slot_id: int, slot ID
        :param prid: str, pipelinerun ID
        :return: True if slot is unlocked successfully, otherwise False
        """
        slot_path = self._get_slot_path(slot_id)
        # Only unlock the slot if it's occupied by this pipelinerun
        cmd = f"grep '{prid}@' {slot_path} && truncate -s 0 {slot_path}"

        try:
            _, _, code = self._flock_run(slot_path, cmd)
        except Exception as e:
            logger.warning("%s: cannot unlock slot %s for pipelinerun %s: %s",
                           self.hostname, slot_id, prid, e)
            return False

        if code == 0:
            logger.info("%s: pipelinerun %s's slot %s is unlocked", self.hostname, prid, slot_id)
            return True

        # XXX: command failed, there are 2 possible reasons, but should we just
        # ignore the exact reason?
        # 1. slot is not occupied by pipelinerun
        # 2. truncate command failed, though unlikely to happen
        return False

    @contextmanager
    def _ssh_session(self):
        """ Create an SSH connection."""
        client = self._open_ssh_session()
        try:
            yield client
        finally:
            client.close()

    @backoff.on_exception(
        backoff.expo,
        RETRY_ON_SSH_EXCEPTIONS,
        factor=BACKOFF_FACTOR,
        max_tries=MAX_RETRIES,
        jitter=None,  # use deterministic backoff, do not apply random jitter
        logger=logger,
    )
    def _open_ssh_session(self):
        """
        Create a new SSH connection and return connection object.
        """
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        logger.debug("%s: opening SSH connection", self.hostname)
        client.connect(self.hostname, username=self.username, key_filename=self.ssh_keyfile)
        return client

    @backoff.on_exception(
        backoff.expo,
        RETRY_ON_SSH_EXCEPTIONS,
        factor=BACKOFF_FACTOR,
        max_tries=MAX_RETRIES,
        jitter=None,  # use deterministic backoff, do not apply random jitter
        logger=logger,
    )
    def _run(self, cmd: str):
        """
        Run a shell command on a remote host

        :return: stdout, stderr and exitcode of shell command
        """
        with self._ssh_session() as session:
            logger.debug("%s: try to run command: %s", self.hostname, cmd)
            _, out, err = session.exec_command(cmd, timeout=SSH_COMMAND_TIMEOUT)
            stdout = out.read().decode().strip()
            stderr = err.read().decode().strip()
            exitcode = out.channel.recv_exit_status()
        return stdout, stderr, exitcode

    def _flock_run(self, lockfile: str, cmd: str):
        """
        Lock a file and run the command

        :param lockfile: str, path to lockfile
        :param cmd: str, command string
        """
        _c = f"flock --conflict-exit-code 42 --timeout {FLOCK_TIMEOUT} {lockfile} -c {quote(cmd)}"
        stdout, stderr, exitcode = self._run(_c)
        # Flock has timeout already, so don't retry, just logging it
        if exitcode == 42:
            logger.warning("%s: unable to acquire lock on %s", self.hostname, lockfile)
        return stdout, stderr, exitcode

    def available_slots(self) -> List[int]:
        """
        Returns id list of available slots
        """
        logger.debug("%s: retrieve list of available slots", self.hostname)

        available_slots = []
        for slot_id in range(self.slots):
            if not self.is_free(slot_id):
                logger.debug("%s: slot %s is not free", self.hostname, slot_id)
                continue
            available_slots.append(slot_id)

        return available_slots


class LockedResource:

    def __init__(self, host: RemoteHost, slot: int, prid: str):
        """
        :param slot: int, Remote host slot ID
        :param prid: str, Pipeline run ID
        """
        self.slot = slot
        self.prid = prid
        self.host = host

    def unlock(self):
        self.host.unlock(self.slot, self.prid)


class RemoteHostsPool:

    def __init__(self, hosts: List[RemoteHost]):
        """
        :param hosts: List[RemoteHost], List of Remote hosts
        """
        self.hosts = hosts

    @classmethod
    def from_config(cls, config: dict):
        """ Instantiate remote hosts loaded from configmap

        :param config: dict, Arch specific remote hosts dictionary from configmap

        Example:

        hostname-remote-host1:
          enabled: true
          auth: qa-vm-secret-filepath
          username: cloud-user
          slots: 3
          ...
        hostname-remote-host2:
          ...
        """
        hosts = []
        for hostname, attr in config.items():
            if not attr.get("enabled", False):
                continue
            host = RemoteHost(
                hostname=hostname, username=attr["username"], ssh_keyfile=attr["auth"],
                slots=attr.get("slots", 1)
            )
            # Check whether host is operational before use it
            if host.is_operational:
                hosts.append(host)

        return cls(hosts)

    def lock_resource(self, prid: str) -> Optional[LockedResource]:
        """
        Lock resource for a pipelinerun

        :param prid: str, pipelinerun ID
        """

        if not self.hosts:
            logger.error("This is no available remote host in pool")
            return None

        resources = []
        for host in self.hosts:
            available_slots = host.available_slots()
            if not available_slots:
                logger.info("%s: no available slots", host.hostname)
                continue
            logger.info("%s: available slots: %s", host.hostname, available_slots)
            resources.append((host, available_slots))

        if not resources:
            logger.error("There is no remote host slot available for pipelinerun %s", prid)
            return None

        # Sort list based on ratio of available_slots/all_slots
        resources.sort(key=lambda x: len(x[1])/x[0].slots, reverse=True)

        # Try to lock a remote host slot for pipelinerun
        for host, slots in resources:
            for slot in slots:
                try:
                    host.lock(slot, prid)
                except Exception as e:
                    # Specific exceptions should be handled in nested methods
                    logger.warning("%s: unable to lock slot %s for pipelinerun %s: %s",
                                   host.hostname, slot, prid, e)
                else:
                    return LockedResource(host, slot, prid)

        logger.info("Cannot find remote host resource for pipelinerun %s", prid)
        return None
