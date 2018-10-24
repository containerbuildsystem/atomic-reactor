"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function


import koji
from requests.exceptions import ConnectionError

import logging
import os
import time

from atomic_reactor.constants import (DEFAULT_DOWNLOAD_BLOCK_SIZE,
                                      HTTP_BACKOFF_FACTOR, HTTP_MAX_RETRIES)

logger = logging.getLogger(__name__)


class KojiUploadLogger(object):
    def __init__(self, logger, notable_percent=10):
        self.logger = logger
        self.notable_percent = notable_percent
        self.last_percent_done = 0

    def callback(self, offset, totalsize, size, t1, t2):  # pylint: disable=W0613
        if offset == 0:
            self.logger.debug("upload size: %.1fMiB", totalsize / 1024.0 / 1024)

        if not totalsize or not t1:
            return

        percent_done = 100 * offset / totalsize
        if (percent_done >= 99 or
                percent_done - self.last_percent_done >= self.notable_percent):
            self.last_percent_done = percent_done
            self.logger.debug("upload: %d%% done (%.1f MiB/sec)",
                              percent_done, size / t1 / 1024 / 1024)


class KojiSessionWrapper(object):
    """
    Wrap all calls to koji.ClientSession methods in a catch/exception block, so that
    improperly handled ConnectionErrors from koji.ClientSession will get retried silently.


    """
    def __init__(self, session):
        self._wrapped_session = session

    def __getattr__(self, name):
        session_attr = getattr(self._wrapped_session, name)
        if callable(session_attr):
            def call_with_catch(*a, **kw):
                retry_delay = HTTP_BACKOFF_FACTOR
                last_exc = None
                for retry in range(HTTP_MAX_RETRIES):
                    try:
                        return session_attr(*a, **kw)
                    except ConnectionError as exc:
                        time.sleep(retry_delay * (2 ** retry))
                        last_exc = exc
                        continue
                raise last_exc
            return call_with_catch
        else:
            return session_attr


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
    :param ssl_certs_dir: str, path to "cert" (required), and "serverca" (optional)
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
        kwargs['cert'] = os.path.join(ssl_certs_dir, 'cert')

        # serverca is not required in newer versions of koji, but if set
        # koji will always ensure file exists
        # NOTE: older versions of koji may require this to be set, in
        # that case, make sure serverca is passed in
        serverca_path = os.path.join(ssl_certs_dir, 'serverca')
        if os.path.exists(serverca_path):
            kwargs['serverca'] = serverca_path

        # Older versions of koji actually require this parameter, even though
        # it's completely ignored.
        kwargs['ca'] = None

        result = session.ssl_login(**kwargs)
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
    session = KojiSessionWrapper(koji.ClientSession(hub_url, opts={'krb_rdns': False}))

    if auth_info is not None:
        koji_login(session, **auth_info)

    return session


class TaskWatcher(object):
    def __init__(self, session, task_id, poll_interval=5):
        self.session = session
        self.task_id = task_id
        self.poll_interval = poll_interval
        self.state = 'CANCELED'

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


def tag_koji_build(session, build_id, target, poll_interval=5):
    logger.debug('Finding build tag for target %s', target)
    target_info = session.getBuildTarget(target)
    build_tag = target_info['dest_tag_name']
    logger.info('Tagging build with %s', build_tag)
    task_id = session.tagBuild(build_tag, build_id)

    task = TaskWatcher(session, task_id, poll_interval=poll_interval)
    task.wait()
    if task.failed():
        raise RuntimeError('Task %s failed to tag koji build' % task_id)

    return build_tag


def get_koji_task_owner(session, task_id, default=None):
    default = {} if default is None else default
    if task_id:
        try:
            koji_task_info = session.getTaskInfo(task_id)
            koji_task_owner = session.getUser(koji_task_info['owner'])
        except Exception:
            logger.exception('Unable to get Koji task owner')
            koji_task_owner = default
    else:
        koji_task_owner = default
    return koji_task_owner


def get_koji_module_build(session, module_spec):
    """
    Get build information from Koji for a module. The module specification must
    include at least name, stream and version. For legacy support, you can omit
    context if there is only one build of the specified NAME:STREAM:VERSION.

    :param session: KojiSessionWrapper, Session for talking to Koji
    :param module_spec: ModuleSpec, specification of the module version
    :return: tuple, a dictionary of information about the build, and
        a list of RPMs in the module build
    """

    if module_spec.context is not None:
        # The easy case - we can build the koji "name-version-release" out of the
        # module spec.
        koji_nvr = "{}-{}-{}.{}".format(module_spec.name,
                                        module_spec.stream.replace("-", "_"),
                                        module_spec.version,
                                        module_spec.context)
        logger.info("Looking up module build %s in Koji", koji_nvr)
        build = session.getBuild(koji_nvr)
    else:
        # Without the context, we have to retrieve all builds for the module, and
        # find the one we want. This is pretty inefficient, but won't be needed
        # long-term.
        logger.info("Listing all builds for %s in Koji", module_spec.name)
        package_id = session.getPackageID(module_spec.name)
        builds = session.listBuilds(packageID=package_id, type='module',
                                    state=koji.BUILD_STATES['COMPLETE'])
        build = None

        for b in builds:
            if '.' in b['release']:
                version, context = b['release'].split('.', 1)
                if version == module_spec.version:
                    if build is not None:
                        raise RuntimeError("Multiple builds found for {}"
                                           .format(module_spec.to_str()))
                    else:
                        build = b

        if build is None:
            raise RuntimeError("No build found for {}".format(module_spec.to_str()))

    archives = session.listArchives(buildID=build['build_id'])
    # The RPM list for the 'modulemd.txt' archive has all the RPMs, recent
    # versions of MBS also write upload 'modulemd.<arch>.txt' archives with
    # architecture subsets.
    archives = [a for a in archives if a['filename'] == 'modulemd.txt']
    assert len(archives) == 1

    rpm_list = session.listRPMs(imageID=archives[0]['id'])

    return build, rpm_list
