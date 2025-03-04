"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.constants import PLUGIN_CHECK_USER_SETTINGS
from atomic_reactor.plugin import Plugin
from atomic_reactor.util import (
    has_operator_appregistry_manifest,
    has_operator_bundle_manifest,
    read_content_sets,
    read_fetch_artifacts_koji,
    read_fetch_artifacts_pnc,
    read_fetch_artifacts_url,
    map_to_user_params,
)

from osbs.utils import Labels


class CheckUserSettingsPlugin(Plugin):
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

    args_from_user_params = map_to_user_params("flatpak")

    def __init__(self, workflow, flatpak=False):
        """
        :param workflow: DockerBuildWorkflow instance
        :param flatpak: bool, if build is for flatpak
        """
        super(CheckUserSettingsPlugin, self).__init__(workflow)

        self.flatpak = flatpak

    def dockerfile_checks(self):
        """Checks for Dockerfile"""
        if self.flatpak:
            self.log.info(
                "Skipping Dockerfile checks because this is flatpak build "
                "without user Dockerfile")
            return

        self.label_version_check()
        self.appregistry_bundle_label_mutually_exclusive()
        self.operator_bundle_from_scratch()

    def label_version_check(self):
        """Check that Dockerfile version has correct name."""
        msg = "Dockerfile version label can't contain '/' character"
        self.log.debug("Running check: %s", msg)

        # any_platform: the version label should be equal for all platforms
        parser = self.workflow.build_dir.any_platform.dockerfile
        dockerfile_labels = parser.labels
        labels = Labels(parser.labels)

        component_label = labels.get_name(Labels.LABEL_TYPE_VERSION)
        label_version = dockerfile_labels[component_label]

        if '/' in label_version:
            raise ValueError(msg)

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

        df_images = self.workflow.data.dockerfile_images
        if not df_images.base_from_scratch or len(df_images.original_parents) > 1:
            raise ValueError(msg)

    def validate_user_config_files(self):
        """Validate some user config files"""
        read_fetch_artifacts_koji(self.workflow)
        read_fetch_artifacts_pnc(self.workflow)
        read_fetch_artifacts_url(self.workflow)
        read_content_sets(self.workflow)

    def resolve_remote_sources_version(self):
        """Resolve which version of remote sources should be used"""
        version = self.workflow.source.config.remote_sources_version
        if not version:
            version = self.workflow.conf.remote_sources_default_version

        self.log.info("Remote sources version to be used: %d", version)

        if self.workflow.remote_sources_version_result:
            with open(self.workflow.remote_sources_version_result, 'w') as f:
                f.write(f"{version}")
                f.flush()
        else:
            self.log.warning("remote_sources_version_result path is not specified, "
                             "result won't be written")

    def run(self):
        """
        run the plugin
        """
        self.dockerfile_checks()
        self.validate_user_config_files()
        self.resolve_remote_sources_version()
