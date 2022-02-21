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
import random
import time
from contextlib import contextmanager
from datetime import datetime
from functools import cached_property
from shlex import quote
from typing import List, Optional, Tuple
from paramiko.channel import ChannelFile  # just for type annotation

SSH_COMMAND_TIMEOUT = 30
SLOTS_RELATIVE_PATH = "osbs_slots"
RETRY_ON_SSH_EXCEPTIONS = (paramiko.ssh_exception.NoValidConnectionsError,
                           paramiko.ssh_exception.SSHException)
BACKOFF_FACTOR = 3
MAX_RETRIES = 3

logger = logging.getLogger(__name__)

__all__ = [
    "RemoteHost",
    "RemoteHostsPool",
    "LockedResource",
]


class RemoteHostError(RuntimeError):
    pass


class SlotLockError(RemoteHostError):
    pass


class SlotReadError(RemoteHostError):
    pass


class SlotWriteError(RemoteHostError):
    pass


class SSHRetrySession(paramiko.SSHClient):
    """ paramiko SSHClient with retry mechanism """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @backoff.on_exception(
        backoff.expo,
        RETRY_ON_SSH_EXCEPTIONS,
        factor=BACKOFF_FACTOR,
        max_tries=MAX_RETRIES,
        jitter=None,  # use deterministic backoff, do not apply random jitter
        logger=logger,
    )
    def exec_command(self, *args, **kwargs):
        return super().exec_command(*args, **kwargs)  # nosec ignore B601

    @backoff.on_exception(
        backoff.expo,
        RETRY_ON_SSH_EXCEPTIONS,
        factor=BACKOFF_FACTOR,
        max_tries=MAX_RETRIES,
        jitter=None,  # use deterministic backoff, do not apply random jitter
        logger=logger,
    )
    def connect(self, *args, **kwargs):
        super().connect(*args, **kwargs)

    def run(self, cmd: str) -> Tuple[str, str, int]:
        _, stdout, stderr = self.exec_command(cmd, timeout=SSH_COMMAND_TIMEOUT)  # nosec ignore B601
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        code = stdout.channel.recv_exit_status()
        return out, err, code


class SlotData:

    def __init__(self, prid: Optional[str] = None, timestamp: Optional[str] = None):
        """ Instantiate slot data with values of prid and timestamp """
        # A valid slot contains empty content or content in format:
        # "prid@timestamp"
        # prid: pipelinerun id
        # timestamp: datetime string in iso format
        self.prid = prid
        self.timestamp = timestamp

    @classmethod
    def from_string(cls, string: Optional[str]):
        """ Instantiate from a string """

        # We don't validate the string here, call is_valid to check slot data
        if not string:
            return cls()

        values = string.split("@")
        prid = values[0]
        timestamp = "".join(values[1:])
        return cls(prid=prid, timestamp=timestamp)

    @property
    def is_empty(self):
        return not any((self.prid, self.timestamp))

    @property
    def is_valid(self):
        # Empty slot data is valid
        if self.is_empty:
            return True

        # String of prid cannot contain "@"
        if not isinstance(self.prid, str) or "@" in self.prid:
            return False

        # Verify timestamp string is valid datetime string
        try:
            datetime.fromisoformat(self.timestamp)
        except ValueError:
            return False
        return True

    def to_string(self):
        if self.is_empty:
            return ""
        return f"{self.prid}@{self.timestamp}"

    @property
    def datetime(self):
        return datetime.fromisoformat(self.timestamp)


class RemoteHost:

    def __init__(self, *, hostname: str, username: str, ssh_keyfile: str, slots: int):
        """ Instantiate RemoteHost with hostname, username, ssh key file and slot number

        :param hostname: str, remote hostname for ssh connection
        :param username: str, username for ssh connection
        :param ssh_keyfile: str, filepath to ssh private key
        :param slots: int, number of max allowed slots on remote host
        """
        self._hostname = hostname
        self._username = username
        self._ssh_keyfile = ssh_keyfile
        self._slots = slots

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
    def slots(self):
        return self._slots

    @cached_property
    def slots_dir(self):
        home, _, _ = self._run("pwd")
        return os.path.join(home, SLOTS_RELATIVE_PATH)

    def _is_valid_slot_id(self, slot_id: int) -> bool:
        """ Check if a slot id is valid """
        valid_slots = list(range(self.slots))
        if slot_id not in valid_slots:
            logger.error("%s: invalid slot id %s, should be in: %s",
                         self.hostname, slot_id, valid_slots)
            return False
        return True

    def _get_slot_path(self, slot_id: int) -> str:
        """ Get the absolute path of slot file """
        return os.path.join(self.slots_dir, f"slot_{slot_id}")

    def _get_slot_lock_path(self, slot_id: int) -> str:
        """ Get the absolute path of slot's lock file """
        return os.path.join(self.slots_dir, f"slot_{slot_id}.lock")

    @backoff.on_exception(
        backoff.expo,
        SlotLockError,
        factor=BACKOFF_FACTOR,
        max_tries=MAX_RETRIES,
        jitter=None,  # use deterministic backoff, do not apply random jitter
        logger=logger,
    )
    def _get_blocking_session_with_locked_slot(
        self, session: SSHRetrySession, slot_id: int
    ) -> Tuple[ChannelFile, ChannelFile, ChannelFile]:
        """
        Lock the slot in SSH session and keep it blocked

        :param session: SSHRetrySession, an SSH session
        :param slot_id: int, slot ID
        :return: A tuple of stdin, stdout, stderr of the running command
        """
        # Run `cat` in the session to keep the slot lock file being locked
        lock_path = quote(self._get_slot_lock_path(slot_id))
        cmd = f"flock --conflict-exit-code 42 --nonblocking {lock_path} cat"

        _errmsg = f"{self.hostname}: failed to acquire lock on slot {slot_id}"
        try:
            logger.info("%s: acquiring lock on slot %s", self.hostname, slot_id)
            stdin, stdout, stderr = session.exec_command(cmd)  # nosec ignore B601
        except Exception as ex:
            raise SlotLockError(_errmsg) from ex

        # A short time sleep to wait for the socket to be ready
        time.sleep(0.1)

        try:
            stdin.write("verify lock\n")
            stdin.flush()
        except OSError as ex:
            stdin.close()
            if stdout.channel.recv_exit_status() == 42:
                _errmsg = f"{_errmsg}: slot is locked by others"
            else:
                stderr = stderr.read().decode().strip()
                if stderr:
                    _errmsg = f"{_errmsg}: {stderr}"
            logger.debug("%s: %s", _errmsg, ex)
            raise SlotLockError(_errmsg) from ex
        else:
            if not stdout.readline():
                if stdout.channel.recv_exit_status() == 42:
                    _errmsg = f"{_errmsg}: slot is locked by others"
                else:
                    _errmsg = f"{_errmsg}: no output from cat command"
                logger.debug(_errmsg)
                raise SlotLockError(_errmsg)

        # So far so good, the session is blocked there with keeping the slot lock
        return stdin, stdout, stderr

    @contextmanager
    def _locked_slot(self, slot_id):
        """ Context manager to return a slot with it's being locked until exit """
        # Open two ssh sessions, one is for reading/writing the slot file,
        # the other one is for keeping the lock for that slot file. The two
        # sessions have same lifecycle, they're closed at the same time when
        # errors happen or exit.
        try:
            # A session to run any commands, especially for reading and
            # writing the slot file
            slot_session = self._open_ssh_session()
            # A special session to keep the lock of the slot
            lock_session = self._open_ssh_session()
        except Exception as ex:
            raise SlotLockError(f"{self.hostname}: failed to open SSH sessions") from ex

        _errmsg = f"{self.hostname}: failed to acquire lock on slot {slot_id}"
        lock_stdin = None
        try:
            lock_stdin, _, _ = self._get_blocking_session_with_locked_slot(
                lock_session, slot_id
            )
            yield HostSlot(self, slot_session, slot_id)
        except Exception as ex:
            raise SlotLockError(_errmsg) from ex
        finally:
            if lock_stdin:
                lock_stdin.close()
            slot_session.close()
            lock_session.close()

    def _run(self, cmd: str):
        """
        Run a shell command on host

        :return: stdout, stderr and exit code of shell command
        """
        with self._ssh_session() as session:
            return session.run(cmd)

    @contextmanager
    def _ssh_session(self):
        """ Create an SSH connection."""
        client = self._open_ssh_session()
        try:
            yield client
        finally:
            client.close()

    def _open_ssh_session(self):
        """
        Create a new SSH connection and return connection object.
        """
        client = SSHRetrySession()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        logger.debug("%s: opening SSH connection", self.hostname)
        client.connect(self.hostname, username=self.username, key_filename=self.ssh_keyfile)
        return client

    @property
    def is_operational(self) -> bool:
        """ Check whether this host is operational """
        try:
            _, _, code = self._run(f"mkdir -p {quote(self.slots_dir)}")
        except Exception as e:
            logger.exception("%s: host is not operational: %s", self.hostname, e)
            return False
        if code != 0:
            logger.error("%s: cannot prepare slots directory", self.hostname)
            return False
        return True

    def is_free(self, slot_id: int) -> bool:
        """ Check whether a slot is in free state

        :param slot_id: int, slot ID
        :return: True if slot is in free state
        :rtype: bool
        """
        if not self._is_valid_slot_id(slot_id):
            return False

        # We don't need to lock the slot to check whether it's free,
        # so using a normal ssh session is good enough
        with self._ssh_session() as session:
            slot = HostSlot(self, session, slot_id)
            return slot.is_free

    @backoff.on_exception(
        backoff.expo,
        (SlotLockError, SlotReadError, SlotWriteError),
        factor=BACKOFF_FACTOR,
        max_tries=MAX_RETRIES,
        jitter=None,  # use deterministic backoff, do not apply random jitter
        logger=logger,
    )
    def lock(self, slot_id: int, prid: str) -> bool:
        """ Lock a slot for a pipelinerun

        :param slot_id: int, slot ID
        :param prid: str, pipelinerun ID
        :return: True if slot is locked for the pipelinerun successfully, otherwise False
        :rtype: bool
        """
        if not self._is_valid_slot_id(slot_id):
            return False

        locked = False
        try:
            with self._locked_slot(slot_id) as slot:
                locked = slot.lock(prid)
        except SlotLockError as ex:
            logger.warning("%s: failed to lock slot %s for pipelinerun %s: %s",
                           self.hostname, slot_id, prid, ex)

        if locked:
            logger.info("%s: slot %s is locked for pipelinerun %s",
                        self.hostname, slot.id, prid)
        else:
            logger.warning("%s: failed to lock slot %s for pipelinerun %s",
                           self.hostname, slot_id, prid)
        return locked

    @backoff.on_exception(
        backoff.expo,
        (SlotLockError, SlotReadError, SlotWriteError),
        factor=BACKOFF_FACTOR,
        max_tries=MAX_RETRIES,
        jitter=None,  # use deterministic backoff, do not apply random jitter
        logger=logger,
    )
    def unlock(self, slot_id: int, prid: str) -> bool:
        """ Unlock a slot for a pipelinerun

        :param slot_id: int, slot ID
        :param prid: str, pipelinerun ID
        :return: True if slot is unlocked for the pipelinerun successfully, otherwise False
        :rtype: bool
        """
        if not self._is_valid_slot_id(slot_id):
            return False

        unlocked = False
        try:
            with self._locked_slot(slot_id) as slot:
                unlocked = slot.unlock(prid)
        except SlotLockError as ex:
            logger.warning("%s: failed to unlock slot %s for pipelinerun %s: %s",
                           self.hostname, slot_id, prid, ex)

        if unlocked:
            logger.info("%s: slot %s is unlocked for pipelinerun %s",
                        self.hostname, slot.id, prid)
        else:
            logger.warning("%s: failed to unlock slot %s for pipelinerun %s",
                           self.hostname, slot_id, prid)
        return unlocked

    def available_slots(self) -> List[int]:
        """ Get slots on host which are in free state """
        logger.debug("%s: retrieve list of available slots", self.hostname)
        available_slots = []
        for slot_id in range(self.slots):
            if not self.is_free(slot_id):
                logger.debug("%s: slot %s is not free", self.hostname, slot_id)
                continue
            available_slots.append(slot_id)

        return available_slots


class HostSlot:

    def __init__(self, host: RemoteHost, session: SSHRetrySession, slot_id: int):
        """ Instantiate host slot with remote host instance, an ssh session and slot id

        :param host: RemoteHost, RemoteHost instance
        :param session: SSHRetrySession, SSHRetrySession instance
        :param slot_id: int, slot ID
        """
        self.host = host
        self.hostname = host.hostname
        self.session = session
        self.id = slot_id
        self.path = os.path.join(self.host.slots_dir, f"slot_{slot_id}")

    @property
    def _data(self) -> SlotData:
        content = self._read()
        return SlotData.from_string(content)

    @property
    def prid(self) -> Optional[str]:
        return self._data.prid

    @property
    def timestamp(self) -> Optional[str]:
        """ Get timestamp value in slot file """
        return self._data.timestamp

    @property
    def datetime(self) -> Optional[datetime]:
        """ Get timestamp value in slot file as a datetime.datetime instance """
        return self._data.datetime

    def _read(self) -> str:
        """ Read content from slot file """
        _errmsg = f"{self.hostname}: cannot read content of slot {self.id}"
        try:
            # Touch the slot file to create it in case it doesn't exist
            slot_path = quote(self.path)
            stdout, stderr, code = self.session.run(f"touch {slot_path} && cat {slot_path}")
        except Exception as ex:
            raise SlotReadError(_errmsg) from ex

        if code != 0:
            _errmsg = f"{_errmsg}: {stderr}" if stderr else _errmsg
            raise SlotReadError(_errmsg)
        return stdout

    def _write(self, data: Optional[str] = None):
        """ Write data to slot file """
        # Empty the file by default
        cmd = f"truncate -s 0 {quote(self.path)}"
        if data:
            cmd = f"echo {quote(data)} > {quote(self.path)}"

        _errmsg = f"{self.hostname}: cannot write data to slot {self.id}"
        try:
            _, stderr, code = self.session.run(cmd)
        except Exception as ex:
            raise SlotWriteError({_errmsg}) from ex

        if code != 0:
            _errmsg = f"{_errmsg}: {stderr}" if stderr else _errmsg
            raise SlotWriteError(_errmsg)

    @property
    def is_valid(self):
        """ Check whether the content is valid """
        return self._data.is_valid

    @property
    def is_free(self) -> bool:
        """ Check whether the slot is in free state """
        return self._data.is_empty

    def is_locked_by(self, prid: str) -> bool:
        """ Check whether the slot is locked by a pipelinerun """
        return self._data.prid == prid

    def lock(self, prid: str) -> bool:
        """ Lock the slot for a pipelinerun """
        if not self.is_free:
            logger.debug("%s: slot %s is not free, unable to lock it",
                         self.hostname, self.id)
            return False

        if not self.is_valid:
            logger.warning("%s: slot %s contains invalid content, it's probably corrupted, "
                           "unable to lock it.", self.hostname, self.id)
            return False

        data = SlotData(prid=prid, timestamp=datetime.utcnow().isoformat())
        self._write(data.to_string())
        return True

    def unlock(self, prid: str) -> bool:
        """ Unlock the slot for a pipelinerun """
        if self.is_free:
            logger.warning("%s: slot %s is free, skip unlocking", self.hostname, self.id)
            # Should we return False instead?
            return True

        if not self.is_valid:
            logger.warning("%s: slot %s contains invalid content, it's probably corrupted, "
                           "unable to unlock it.", self.hostname, self.id)
            return False

        if not self.is_locked_by(prid):
            logger.warning("%s: cannot unlock slot %s, it's not locked by %s",
                           self.hostname, self.id, prid)
            return False

        # Empty the slot
        self._write()
        return True


class LockedResource:

    def __init__(self, host: RemoteHost, slot: int, prid: str):
        """ Instantiate a locked resource with remote host, slot id and pipelinerun id

        :param host: RemoteHost, RemoteHost instance
        :param slot: int, slot ID
        :param prid: str, pipeline run ID
        """
        self.host = host
        self.slot = slot
        self.prid = prid

    def unlock(self):
        """ Unlock the resource for pipelinerun """
        self.host.unlock(self.slot, self.prid)


class RemoteHostsPool:

    def __init__(self, hosts: List[RemoteHost]):
        """
        :param hosts: List[RemoteHost], List of Remote hosts
        """
        self.hosts = hosts

    @classmethod
    def from_config(cls, config: dict):
        """ Instantiate remote hosts loaded from a config in dict format

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
            logger.error("There is no available remote host in pool")
            return None

        resources = []
        for host in self.hosts:
            available_slots = host.available_slots()
            if not available_slots:
                logger.info("%s: no available slots", host.hostname)
                continue
            logger.info("%s: available slots: %s", host.hostname, available_slots)
            # random.shuffle the slots to reduce the chance of multiple clients
            # trying to lock the free slots in the same order
            random.shuffle(available_slots)
            resources.append((host, available_slots))

        if not resources:
            logger.error("There is no remote host slot available for pipelinerun %s", prid)
            return None

        # Sort list based on ratio of available_slots/all_slots
        resources.sort(key=lambda x: len(x[1])/x[0].slots, reverse=True)

        # Try to lock a remote host slot for pipelinerun
        for host, slots in resources:
            for slot in slots:
                locked = False
                try:
                    locked = host.lock(slot, prid)
                except Exception as ex:
                    # Specific exceptions should be handled in nested methods
                    logger.warning("%s: unable to lock slot %s for pipelinerun %s: %s",
                                   host.hostname, slot, prid, ex)
                if locked:
                    return LockedResource(host, slot, prid)

        logger.info("Cannot find remote host resource for pipelinerun %s", prid)
        return None
