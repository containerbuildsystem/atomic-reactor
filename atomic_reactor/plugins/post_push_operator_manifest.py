"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import absolute_import

import os

from atomic_reactor.constants import (
    DEFAULT_DOWNLOAD_BLOCK_SIZE,
    PLUGIN_PUSH_OPERATOR_MANIFESTS_KEY,
    OPERATOR_MANIFESTS_ARCHIVE,
)
from atomic_reactor.utils.omps import OMPS, OMPSError
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.plugins.build_orchestrate_build import get_koji_upload_dir
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild
from atomic_reactor.plugins.pre_reactor_config import (
    get_omps_config,
    get_koji_path_info,
)
from atomic_reactor.util import (
    is_isolated_build,
    is_scratch_build,
    has_operator_appregistry_manifest,
    has_operator_bundle_manifest,
    get_retrying_requests_session,
)


class PushOperatorManifestsPlugin(PostBuildPlugin):
    """
    Push operator manifest to appregistry via OMPS service
    """
    key = PLUGIN_PUSH_OPERATOR_MANIFESTS_KEY
    is_allowed_to_fail = False

    DOWNLOAD_DIR = 'operators_artifacts'

    def should_run(self):
        """
        Check if the plugin should run or skip execution.

        :return: bool, False if plugin should skip execution
        """
        if not self.is_in_orchestrator():
            self.log.warning("%s plugin set to run on worker. Skipping", self.key)
            return False

        if not get_omps_config(self.workflow, None):
            self.log.info("Integration with OMPS is not configured. Skipping")
            return False

        if has_operator_bundle_manifest(self.workflow):
            self.log.info(
                "Operator bundle format is not compatible with appregistry. "
                "Skipping publishing into appregistry.")
            return False

        if not has_operator_appregistry_manifest(self.workflow):
            self.log.info("Not an operator build. Skipping")
            return False

        if is_scratch_build(self.workflow):
            self.log.info('Scratch build. Skipping')
            return False

        if is_rebuild(self.workflow):
            self.log.info('Autorebuild. Skipping')
            return False

        if is_isolated_build(self.workflow):
            self.log.info('Isolated build. Skipping')
            return False

        return True

    def get_koji_operator_manifest_url(self):
        """Construct URL for downloading manifest from koji task work server_dir

        :rtype: str
        :return: URL of koji operator manifests archive
        """

        server_dir = get_koji_upload_dir(self.workflow)

        pathinfo = get_koji_path_info(self.workflow)
        file_url = "{}/work/{}/{}".format(
            pathinfo.topdir, server_dir, OPERATOR_MANIFESTS_ARCHIVE)

        return file_url

    def download_file(self, url, dest_filename, insecure=False):
        """Downloads file specified by URL

        :param url: file url
        :param dest_filename: filename to be used for downloaded content
        :param insecure: download file without cert validation

        :return: file path of downloaded content
        """
        self.log.debug('Downloading file: %s', url)

        workdir = self.workflow.source.get_build_file_path()[1]
        dest_dir = os.path.join(workdir, self.DOWNLOAD_DIR)
        dest_path = os.path.join(dest_dir, dest_filename)
        if not os.path.exists(dest_dir):
            os.makedirs(dest_dir)

        req_session = get_retrying_requests_session()
        request = req_session.get(url, stream=True,
                                  verify=not insecure)
        request.raise_for_status()

        with open(dest_path, 'wb') as f:
            for chunk in request.iter_content(chunk_size=DEFAULT_DOWNLOAD_BLOCK_SIZE):
                f.write(chunk)

        self.log.debug('Download finished: %s', dest_path)

        return dest_path

    def run(self):
        """
        Run the plugin.

        :return: dictionary with following keys:
          - endpoint: appregistry API endpoint
          - registryNamespace: namespace/organization where operator manifest was pushed
          - repository: repository name
          - release: release version (related to appregistry repository)
        """

        if not self.should_run():
            return

        omps_config = get_omps_config(self.workflow)
        omps = OMPS.from_config(omps_config)

        operator_manifest_url = self.get_koji_operator_manifest_url()
        operator_manifests_path = self.download_file(
            operator_manifest_url, OPERATOR_MANIFESTS_ARCHIVE,
            insecure=omps_config.get('koji_insecure', False)
        )

        try:
            with open(operator_manifests_path, 'rb') as fb:
                result = omps.push_archive(fb)
        except OMPSError as e:
            msg = "Failed to push operator manifests: {}".format(e)
            self.log.error(msg)
            raise RuntimeError(msg)

        try:
            os.remove(operator_manifests_path)
        except OSError as e:
            self.log.warning("Cleanup of the downloaded archive failed: %s", e)

        org = result['organization']
        repo = result['repo']
        release = result['version']
        self.log.info(
            "Operator manifest pushed to %s/%s as release %s",
            org, repo, release
        )
        return {
            "endpoint": omps_config['appregistry_url'],
            "registryNamespace": org,
            "repository": repo,
            "release": release,
        }
