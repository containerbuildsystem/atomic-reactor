"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.constants import PLUGIN_KOJI_TAG_BUILD_KEY
from atomic_reactor.config import get_koji_session
from atomic_reactor.utils.koji import tag_koji_build
from atomic_reactor.util import is_scratch_build
from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.plugins.exit_koji_import import KojiImportPlugin


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

    key = PLUGIN_KOJI_TAG_BUILD_KEY
    is_allowed_to_fail = False

    def __init__(self, workflow, target=None, poll_interval=5):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :param target: str, koji target
        :param poll_interval: int, seconds between Koji task status requests
        """
        super(KojiTagBuildPlugin, self).__init__(workflow)

        self.target = target
        self.poll_interval = poll_interval

    def run(self):
        """
        Run the plugin.
        """
        if self.workflow.build_process_failed:
            self.log.info('Build failed, skipping koji tagging')
            return

        if is_scratch_build(self.workflow):
            self.log.info('scratch build, skipping plugin')
            return

        if not self.target:
            self.log.info('no koji target provided, skipping plugin')
            return

        build_id = self.workflow.exit_results.get(KojiImportPlugin.key)
        if not build_id:
            self.log.info('No koji build from %s', KojiImportPlugin.key)
            return

        session = get_koji_session(self.workflow.conf)
        build_tag = tag_koji_build(session, build_id, self.target,
                                   poll_interval=self.poll_interval)

        return build_tag
