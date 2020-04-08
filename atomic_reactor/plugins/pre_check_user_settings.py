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

    def __init__(self, tasker, workflow, flatpak=False):
        """
        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param flatpak: bool, if build is for flatpak
        """
        super(CheckUserSettingsPlugin, self).__init__(tasker, workflow)

        self.flatpak = flatpak

    def dockerfile_checks(self):
        """Checks for Dockerfile"""
        if self.flatpak:
            self.log.info(
                "Skipping Dockerfile checks because this is flatpak build "
                "without user Dockerfile")
            return

        self.appregistry_bundle_label_mutually_exclusive()
        self.operator_bundle_from_scratch()

    def appregistry_bundle_label_mutually_exclusive(self):
        """Labels com.redhat.com.delivery.appregistry and
        com.redhat.delivery.operator.bundle
        are mutually exclusive. Fail when both are specified.
        """
        msg = (
            "only one of labels com.redhat.com.delivery.appregistry "
            "and com.redhat.delivery.operator.bundle is allowed"
        )
        self.log.debug("Running check: %s", msg)
        if (
            has_operator_appregistry_manifest(self.workflow) and
            has_operator_bundle_manifest(self.workflow)
        ):
            raise ValueError(msg)

    def operator_bundle_from_scratch(self):
        """Only from scratch image can be used for operator bundle build"""
        msg = "Operator bundle build can be only 'FROM scratch' build (single stage)"
        self.log.debug("Running check: %s", msg)

        if not has_operator_bundle_manifest(self.workflow):
            return

        if (
            not self.workflow.builder.base_from_scratch or
            len(self.workflow.builder.parents_ordered) > 1
        ):
            raise ValueError(msg)

    def run(self):
        """
        run the plugin
        """
        self.dockerfile_checks()
