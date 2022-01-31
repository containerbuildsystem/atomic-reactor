"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import os
import logging
import json
import paramiko
import backoff
import jsonschema
from typing import List, Any, Dict, Tuple
from datetime import datetime
from shlex import quote

PARAMIKO_MAX_RETRIES = 3
PARAMIKO_BACKOFF_FACTOR = 1

ROOT_PATH = 'resource_manager/'

RESOURCE_LOCK_SCHEMA = 'schemas/resource-lock-content.json'

logger = logging.getLogger(__name__)


class Host:
    '''
    Remote host abstract class
    '''
    def __init__(self, username: str, hostname: str):
        self.username = username
        self.hostname = hostname
        self.slots: List[Dict[str, Any]] = []
        self.free_slots: List[int] = []

    def _read_info_data(self):
        cmd = f"cat {ROOT_PATH}info.json"
        data = self._ssh_run_remote_command(cmd)
        return data

    def _read_slot_data(self, slot: int):
        cmd = f"cat {ROOT_PATH}slot_{slot}.json"
        data = self._ssh_run_remote_command(cmd)
        return data

    def _validate_resource_lock(self, payload: dict):
        with open(RESOURCE_LOCK_SCHEMA, 'r') as f:
            schema_data = f.read()
        schema = json.loads(schema_data)
        try:
            jsonschema.validate(payload, schema)
            return True
        except jsonschema.ValidationError as err:
            logger.error(err)
        return False

    def check_avail(self):
        try:
            data = self._read_info_data()
            if data == '':
                logger.warning("%s hasn't been deployed yet", self.hostname)
                return False
            else:
                # parse slot count, arch
                meta = json.loads(data)
                logger.debug("%s is up", meta['hostname'])
                # check slots status
                self.free_slots = []
                for i in range(meta.get('max_slot_count', 1)):
                    slot = self._read_slot_data(i)
                    if slot == b'0\n':
                        state = 'free'
                        prid = ''
                        self.free_slots.append(i)
                    elif slot == "":
                        logger.warning("host: %s slot: %s has invalid state", self.hostname, i)
                        state = 'unknown'
                        prid = 'unknown'
                    else:
                        state = 'running'
                        slot_data = json.loads(slot)
                        if self._validate_resource_lock(slot_data):
                            prid = slot_data.get("prid", "unknown")
                        else:
                            logger.warning("host: %s slot: %s has invalid state", self.hostname, i)
                            state = 'invalid'
                            prid = 'invalid'
                    logger.debug("%s slot %s is %s", meta['hostname'], i, state)
                    self.slots.append({'sid': i, 'state': state, 'prid': prid})
        except Exception as err:
            logger.error(err)
            logger.error("Availiability check failed fo host: %s", self.hostname)
        return self.free_slots

    def _write_free_slot(self, slot: int):
        cmd = f'echo \'0\' > {ROOT_PATH}slot_{slot}.json'
        data = self._ssh_run_remote_command(cmd)
        return data

    def unlock(self, slot: int, prid: str):
        logger.debug('Unlocking')
        retries = 0
        while retries < 3:
            ret = self._write_free_slot(slot)
            if ret == b'':
                logger.info('Unlocked')
                return True
            else:
                logger.debug('Unlocking failed, retrying')
                retries += 1
        return False

    def lock(self, slot: int, prid: str):
        logger.debug("Aquiring lock")
        payload = json.dumps({"prid": prid, "locked": str(datetime.now())})
        retries = 0
        while retries < 3:
            filename = os.path.join(ROOT_PATH, f'slot_{slot}.json')
            _cmd = 'if grep -qw 0 {0} ; then echo {1} > {0} ;else exit 1; fi'.format(
                    filename,
                    quote(payload)
                    )
            cmd = 'flock {} -c {}; echo $?'.format(filename, quote(_cmd))
            logger.debug(cmd)
            ret = self._ssh_run_remote_command(cmd)
            if ret == b'0\n':
                logger.info('Locked')
                return True
            else:
                logger.debug('Locking failed, retrying')
                retries += 1
        logger.info('Locking failed')
        raise Exception('Locking failed')

    @backoff.on_exception(
        backoff.expo,
        Exception,
        factor=PARAMIKO_BACKOFF_FACTOR,
        max_tries=PARAMIKO_MAX_RETRIES + 1,  # total tries is N retries + 1 initial attempt
        jitter=None,  # use deterministic backoff, do not apply random jitter
    )
    def _ssh_run_remote_command(self, cmd: str):
        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh_client.connect(hostname=self.hostname,
                           username=self.username)
        cmd_out = ssh_client.exec_command(cmd)
        out = cmd_out[1].read()
        error = cmd_out[2].read()
        if error:
            raise Exception('There was an error pulling the runtime: {}'.format(error.decode()))
        ssh_client.close()
        return out


class ResourceManager:
    '''
    Manages build resources
    '''

    def __init__(self, hosts: List[Host]):
        self.hosts = hosts

    def find(self) -> List[Host]:
        try:
            resources = []
            for host in self.hosts:
                host.check_avail()
                if len(host.free_slots) > 0:
                    resources.append(host)
            sorted_result = sorted(resources, key=lambda h: len(h.free_slots))
            logger.debug('sorted result: %s', sorted_result)
            return sorted_result
        except Exception as err:
            logger.debug(err)
            logger.error("Unable to read hosts file")
            return []

    def obtain_free_resource(self, prid: str) -> Tuple[Host, int]:
        availiable_hosts = self.find()
        for host in availiable_hosts:
            for sid in host.free_slots:
                if host.lock(sid, prid):
                    return host, sid
        raise Exception('No availiable host found')


if __name__ == '__main__':
    my_hosts = [Host('username', 'hostname')]
    mngr = ResourceManager(my_hosts)
    # locking
    free_host, slot = mngr.obtain_free_resource('unique-name-for-pipeline-run')

    # Unlocking
    free_host.unlock(slot, 'unique-name-for-pipeline-run')
