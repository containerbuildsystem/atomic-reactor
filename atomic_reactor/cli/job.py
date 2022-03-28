"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import logging

from atomic_reactor.config import Configuration, get_openshift_session
from atomic_reactor.utils import remote_host


logger = logging.getLogger(__name__)


def remote_hosts_unlocking_recovery(job_args: dict) -> None:
    config = Configuration(config_path=job_args['config_file'])
    osbs = get_openshift_session(config, job_args['namespace'])

    remote_host_pools = config.remote_hosts.get("pools")

    for platform in remote_host_pools.keys():
        platform_pool = remote_host.RemoteHostsPool.from_config(config.remote_hosts, platform)

        for host in platform_pool.hosts:
            logger.info("Checking occupied slots for platform: %s on host: %s",
                        platform, host.hostname)

            for slot in range(host.slots):
                prid = host.prid_in_slot(slot)

                if not prid:
                    continue

                logger.info("slot: %s is occupied by prid: %s", slot, prid)

                if not osbs.build_not_finished(prid):
                    logger.info('prid: %s finished, will unlock slot: %s', prid, slot)
                    host.unlock(slot, prid)
