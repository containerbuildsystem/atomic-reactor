"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

from atomic_reactor.constants import PLUGIN_CHECK_USER_SETTINGS
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import (
    has_operator_appregistry_manifest,
    has_operator_bundle_manifest,
)


class CheckUserSettingsPlugin(PreBuildPlugin):
    """
    Pre plugin will check user settings on early phase to fail early and save resources.

    Aim of this plugin to checks:
    * Dockerfile
    * container.yaml
    * git repo

    for incorrect options or mutually exclusive options
    """
    key = PLUGIN_CHECK_USER_SETTINGS
    is_allowed_to_fail = False

    def dockerfile_checks(self):
        """Checks for Dockerfile"""
        self.appregistry_bundle_label_mutually_exclusive()

    def appregistry_bundle_label_mutually_exclusive(self):
        """Labels com.redhat.com.delivery.appregistry and
        com.redhat.delivery.operator.bundle
        are mutually exclusive. Fail when both are specified.
        """
        msg = (
            "only one of labels com.redhat.com.delivery.appregistry"
            "and com.redhat.delivery.operator.bundle is allowed"
        )
        self.log.debug("Running check: %s", msg)
        if (
            has_operator_appregistry_manifest(self.workflow) and
            has_operator_bundle_manifest(self.workflow)
        ):
            raise ValueError(msg)

    def run(self):
        """
        run the plugin
        """
        self.dockerfile_checks()
