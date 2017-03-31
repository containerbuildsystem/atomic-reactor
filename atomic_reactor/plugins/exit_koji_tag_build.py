"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.plugins.exit_koji_promote import KojiPromotePlugin
from atomic_reactor.koji_util import create_koji_session, TaskWatcher


class KojiTagBuildPlugin(ExitPlugin):
    """
    Tag build in koji

    Authentication is with Kerberos unless the koji_ssl_certs
    configuration parameter is given, in which case it should be a
    path at which 'cert', 'ca', and 'serverca' are the certificates
    for SSL authentication.

    If Kerberos is used for authentication, the default principal will
    be used (from the kernel keyring) unless both koji_keytab and
    koji_principal are specified. The koji_keytab parameter is a
    keytab name like 'type:name', and so can be used to specify a key
    in a Kubernetes secret by specifying 'FILE:/path/to/key'.
    """

    key = "koji_tag_build"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, kojihub, target,
                 koji_ssl_certs=None, koji_proxy_user=None,
                 koji_principal=None, koji_keytab=None,
                 poll_interval=5):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param kojihub: string, koji hub (xmlrpc)
        :param target: str, koji target
        :param koji_ssl_certs: str, path to 'cert', 'ca', 'serverca'
        :param koji_proxy_user: str, user to log in as (requires hub config)
        :param koji_principal: str, Kerberos principal (must specify keytab)
        :param koji_keytab: str, keytab name (must specify principal)
        :param poll_interval: int, seconds between Koji task status requests
        """
        super(KojiTagBuildPlugin, self).__init__(tasker, workflow)

        if bool(koji_principal) != bool(koji_keytab):
            raise RuntimeError('specify both koji_principal and koji_keytab '
                               'or neither')

        self.kojihub = kojihub
        self.koji_auth = {
            "proxyuser": koji_proxy_user,
            "ssl_certs_dir": koji_ssl_certs,
            # krbV python library throws an error if these are unicode
            "krb_principal": str(koji_principal),
            "krb_keytab": str(koji_keytab)
        }

        self.target = target
        self.poll_interval = poll_interval

    def run(self):
        """
        Run the plugin.
        """
        if self.workflow.build_process_failed:
            self.log.info('Build failed, skipping koji tagging')
            return

        build_id = self.workflow.exit_results.get(KojiPromotePlugin.key)
        if not build_id:
            self.log.info('No koji build from %s', KojiPromotePlugin.key)
            return

        session = create_koji_session(self.kojihub, self.koji_auth)

        self.log.debug('Finding build tag for target %s', self.target)
        target_info = session.getBuildTarget(self.target)
        build_tag = target_info['dest_tag_name']
        self.log.info('Tagging build with %s', build_tag)
        task_id = session.tagBuild(build_tag, build_id)
        task = TaskWatcher(session, task_id,
                           poll_interval=self.poll_interval)
        task.wait()
        if task.failed():
            raise RuntimeError('Task %s failed to tag koji build' % task_id)

        return build_tag
