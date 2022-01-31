"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import backoff
import json
import jsonschema
import logging
import paramiko
import random
import os
from contextlib import contextmanager
from datetime import datetime
from shlex import quote
from typing import List

RESOURCE_LOCK_SCHEMA = 'schemas/resource-lock-content.json'
SSH_COMMAND_TIMEOUT = 45
TIMEOUT_FAIL_BUILD = 3000
RELATIVE_PATH = 'resource_manager'
RESOURCE_INFO_JSON = 'info.json'
RETRY_ON_SSH_EXCEPTIONS = (paramiko.ssh_exception.NoValidConnectionsError,
                           paramiko.ssh_exception.SSHException)
BACKOFF_FACTOR = 5
MAX_RETRIES = 3

logger = logging.getLogger(__name__)


class ResourceManagerError(RuntimeError):
    """ Base module exception """


class LockError(ResourceManagerError):
    """ Attempt for resource locking returned non-zero exitcode """


class LockRetry(ResourceManagerError):
    """ Retry locking, no resources were available """


class RemoteHost:

    def __init__(self, *, hostname: str, username: str, ssh_keyfile: str):
        """
        :param hostname: str, remote hostname for ssh connection
        :param username: str, username for ssh connection
        :param ssh_keyfile: str, filepath to ssh private key
        """
        self._hostname = hostname
        self._username = username
        self._ssh_keyfile = ssh_keyfile

    @property
    def hostname(self):
        return self._hostname

    @property
    def username(self):
        return self._username

    @property
    def ssh_keyfile(self):
        return self._ssh_keyfile

    def _validate_resource_lock(self, payload: dict):
        """ Validate resource lock json file against schema """
        with open(RESOURCE_LOCK_SCHEMA, 'r') as f:
            schema_data = f.read()
        schema = json.loads(schema_data)
        try:
            jsonschema.validate(payload, schema)
        except jsonschema.ValidationError as err:
            logger.warning("Invalid json file: %s", err)

    @contextmanager
    def _ssh_session(self):
        """ Create an SSH connection."""
        try:
            client = self._open_ssh_session()
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
        logger.debug("Open SSH connection in remote host %s", self.hostname)
        client.connect(
            self.hostname, username=self.username, key_filename=self.ssh_keyfile)
        return client

    @backoff.on_exception(
        backoff.expo,
        RETRY_ON_SSH_EXCEPTIONS,
        factor=BACKOFF_FACTOR,
        max_tries=MAX_RETRIES,
        jitter=None,  # use deterministic backoff, do not apply random jitter
        logger=logger,
    )
    def _ssh_run_remote_cmd(self, client, cmd: List):
        """
        Run a Shell command on a remote host

        :return: stdout, stderr and exitcode of shell command
        """
        logger.debug("Try to run command %s on remote host %s", cmd, self.hostname)
        _, stdout, stderr = client.exec_command(cmd, timeout=SSH_COMMAND_TIMEOUT)
        return stdout, stderr, stdout.channel.recv_exit_status()

    def _ssh_run_remote_cmd_to_get_json(self, client, cmd: List):
        _, stdout, _ = self._ssh_run_remote_cmd(client, cmd)
        try:
            json_stdout = json.loads(stdout)
        except ValueError as ex:  # Not valid JSON
            logger.warning("Json load failed. Not a valid JSON. %s", ex)
            return None
        except TypeError as ex:  # Not an object
            logger.warning("Json load failed. Not an object. %s", ex)
            return None
        return json_stdout

    def available_slots(self) -> List[int]:
        """
        Returns list of available slots
        """
        logger.debug("Retrieve list of available slots on host %s", self.hostname)

        with self._ssh_session() as client:
            cmd = self.get_cmd_to_read_from_json(RESOURCE_INFO_JSON)
            resource_info = self._ssh_run_remote_cmd_to_get_json(client, cmd)

            available_slots = []
            for slot_id in range(resource_info.get('max_slot_count', 1)):
                cmd = self.get_cmd_to_read_from_json(f'slot_{slot_id}.json')
                slot = self._ssh_run_remote_cmd_to_get_json(client, cmd)

                if slot is None:  # Json load failed - file on remote host might be corrupted
                    logger.warning("%s - slot %s - json load failed",
                                   resource_info['hostname'], slot_id)
                elif slot:  # Non-empty json file was loaded
                    prid = slot.get('prid')
                    logger.debug("%s - slot %s is occupied by %s",
                                 resource_info['hostname'], slot_id, prid)
                    self._validate_resource_lock(slot)
                elif not slot:  # Empty json file was loaded
                    logger.debug("%s - slot %s is available",
                                 resource_info['hostname'], slot_id)
                    available_slots.append(slot_id)
                else:  # Undefined state - should not happen
                    logger.warning("%s - slot %s invalid state!",
                                   resource_info['hostname'], slot_id)

        return available_slots

    def get_cmd_to_read_from_json(self, file_name: str):
        """ Create a command to retrieve content of json file from remote host """
        return ['cat', os.path.join(RELATIVE_PATH, file_name)]

    def get_cmd_to_write_empty_slot(self, slot_id: int):
        """ Create a command to unlock slot on remote host """
        return ['echo', '{}', '>', os.path.join(RELATIVE_PATH, f'slot_{slot_id}.json')]

    def get_cmd_to_write_slot_in_progress(self, file_name: str, payload: str):
        """ Create a command to lock slot for pipelinerun on remote host """
        filepath = os.path.join(RELATIVE_PATH, file_name)

        # TBD: Test with remote host that this is actually working with Paramiko
        _cmd = (f'if grep -qw {{}} {filepath} ; then echo {quote(payload)} > '
                f'{filepath} ;else exit 1; fi')
        return f'flock {filepath} -c {quote(_cmd)}'

    @backoff.on_exception(
        backoff.expo,
        LockError,
        factor=BACKOFF_FACTOR,
        max_tries=MAX_RETRIES,
        jitter=None,  # use deterministic backoff, do not apply random jitter
        logger=logger,
    )
    def lock(self, slot_id: int, prid: str):
        logger.debug("Locking slot %s on host %s with pipelinerun ID %s",
                     slot_id, self.hostname, prid)

        payload = json.dumps({'prid': prid, 'locked': str(datetime.utcnow().isoformat())})

        with self._ssh_session() as client:
            cmd = self.get_cmd_to_write_slot_in_progress(f'slot_{slot_id}.json', payload)
            _, _, exitcode = self._ssh_run_remote_cmd(client, cmd)

            if exitcode != 0:
                raise LockError(f'Attempt to lock slot {slot_id} with pipelinerun ID {prid} '
                                'returned non-zero exitcode')

        logger.debug("%s - slot %s locked with pipelinerun ID %s", self.hostname, slot_id, prid)

    def unlock(self, slot_id: int, prid: int):
        logger.debug("Unlocking slot %s on host %s with pipelinerun ID %s",
                     slot_id, self.hostname, prid)

        with self._ssh_session() as client:
            cmd = self.get_cmd_to_read_from_json(f'slot_{slot_id}.json')
            slot = self._ssh_run_remote_cmd_to_get_json(client, cmd)

            if slot:
                self._validate_resource_lock(slot)

                if slot['prid'] != prid:
                    logger.warning("%s - slot %s, Found pipelinerun ID: %s, expected %s. Unlocking"
                                   " skipped!", self.hostname, slot_id, slot['prid'], prid)
                    return

                cmd = self.get_cmd_to_write_empty_slot(slot_id)
                _, _, exitcode = self._ssh_run_remote_cmd(client, cmd)

                if exitcode != 0:
                    logger.warning("Unlocking slot %s on host %s failed!", slot_id, self.hostname)
            else:
                logger.warning("%s - slot %s is available. Unlocking skipped!",
                               self.hostname, slot_id)


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
          ...
        hostname-remote-host2:
          ...
        """

        # Add enabled clusters to list hosts
        hosts = [RemoteHost(hostname=hostname, username=attr['username'], ssh_keyfile=attr['auth'])
                 for hostname, attr in config.items() if attr['enabled']]

        return cls(hosts)

    @backoff.on_exception(
        backoff.constant,
        LockRetry,
        max_time=TIMEOUT_FAIL_BUILD,
        jitter=None,
    )
    def lock_resource(self, prid: str) -> LockedResource:
        """ Lock resources for build """

        # Slots should be randomized here to avoid always locking the lowest numbers first
        resources = []
        for host in self.hosts:
            available_slots = host.available_slots()
            random.shuffle(available_slots)
            resources.append((host, available_slots))

        # Sort list based on number of available slots
        resources.sort(key=lambda x: len(x[1]), reverse=True)

        # Try to lock resources
        for host, slots in resources:
            for slot in slots:
                try:
                    host.lock(slot, prid)
                except Exception as ex:
                    # Specific exceptions should be handled in nested methods
                    logger.warning("Locking failed on slot %s, host %s - %s", slot, host, ex)
                else:
                    return LockedResource(host, slot, prid)

        raise LockRetry(f'No remote host resources were available for pipelinerun ID {prid}')
