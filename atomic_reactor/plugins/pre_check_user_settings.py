"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.constants import (PLUGIN_CHECK_USER_SETTINGS, CONTAINER_DOCKERPY_BUILD_METHOD,
                                      CONTAINER_IMAGEBUILDER_BUILD_METHOD)
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import (
    has_operator_appregistry_manifest,
    has_operator_bundle_manifest,
    read_content_sets,
    read_fetch_artifacts_koji,
    read_fetch_artifacts_url
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
        self.multistage_docker_api_check()

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
            not self.workflow.builder.dockerfile_images.base_from_scratch or
            len(self.workflow.builder.dockerfile_images.original_parents) > 1
        ):
            raise ValueError(msg)

    def multistage_docker_api_check(self):
        """Check if multistage build isn't tried with docker_api"""
        if self.workflow.builder.tasker.build_method != CONTAINER_DOCKERPY_BUILD_METHOD:
            return

        if len(self.workflow.builder.dockerfile_images.original_parents) > 1:
            msg = "Multistage builds can't be built with docker_api," \
                  "use 'image_build_method' in container.yaml " \
                  "with '{}'".format(CONTAINER_IMAGEBUILDER_BUILD_METHOD)
            raise RuntimeError(msg)

    def validate_user_config_files(self):
        """Validate some user config files"""
        read_fetch_artifacts_koji(self.workflow)
        read_fetch_artifacts_url(self.workflow)
        read_content_sets(self.workflow)

    def run(self):
        """
        run the plugin
        """
        self.dockerfile_checks()
        self.validate_user_config_files()
