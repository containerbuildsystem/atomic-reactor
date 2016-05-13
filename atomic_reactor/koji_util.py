"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function


import koji
import logging
import os
import time

from atomic_reactor.constants import DEFAULT_DOWNLOAD_BLOCK_SIZE


logger = logging.getLogger(__name__)


def koji_login(session,
               proxyuser=None,
               ssl_certs_dir=None,
               krb_principal=None,
               krb_keytab=None):
    """
    Choose the correct login method based on the available credentials,
    and call that method on the provided session object.

    :param session: koji.ClientSession instance
    :param proxyuser: str, proxy user
    :param ssl_certs_dir: str, path to "cert", "ca", and "serverca"
    :param krb_principal: str, name of Kerberos principal
    :param krb_keytab: str, Kerberos keytab
    :return: None
    """

    kwargs = {}
    if proxyuser:
        kwargs['proxyuser'] = proxyuser

    if ssl_certs_dir:
        # Use certificates
        logger.info("Using SSL certificates for Koji authentication")
        result = session.ssl_login(os.path.join(ssl_certs_dir, 'cert'),
                                   os.path.join(ssl_certs_dir, 'ca'),
                                   os.path.join(ssl_certs_dir, 'serverca'),
                                   **kwargs)
    else:
        # Use Kerberos
        logger.info("Using Kerberos for Koji authentication")
        if krb_principal and krb_keytab:
            kwargs['principal'] = krb_principal
            kwargs['keytab'] = krb_keytab

        result = session.krb_login(**kwargs)

    if not result:
        raise RuntimeError('Unable to perform Koji authentication')

    return result


def create_koji_session(hub_url, auth_info=None):
    """
    Creates and returns a Koji session. If auth_info
    is provided, the session will be authenticated.

    :param hub_url: str, Koji hub URL
    :param auth_info: dict, authentication parameters used for koji_login
    :return: koji.ClientSession instance
    """
    session = koji.ClientSession(hub_url)

    if auth_info is not None:
        koji_login(session, **auth_info)

    return session


class TaskWatcher(object):
    def __init__(self, session, task_id, poll_interval=5):
        self.session = session
        self.task_id = task_id
        self.poll_interval = poll_interval

    def wait(self):
        logger.debug("waiting for koji task %r to finish", self.task_id)
        while not self.session.taskFinished(self.task_id):
            time.sleep(self.poll_interval)

        logger.debug("koji task is finished, getting info")
        task_info = self.session.getTaskInfo(self.task_id, request=True)
        self.state = koji.TASK_STATES[task_info['state']]
        return self.state

    def failed(self):
        return self.state in ['CANCELED', 'FAILED']


def stream_task_output(session, task_id, file_name,
                       blocksize=DEFAULT_DOWNLOAD_BLOCK_SIZE):
    """
    Generator to download file from task without loading the whole
    file into memory.
    """
    logger.debug('Streaming {} from task {}'.format(file_name, task_id))
    offset = 0
    contents = '[PLACEHOLDER]'
    while contents:
        contents = session.downloadTaskOutput(task_id, file_name, offset,
                                              blocksize)
        offset += len(contents)
        if contents:
            yield contents

    logger.debug('Finished streaming {} from task {}'.format(file_name, task_id))
