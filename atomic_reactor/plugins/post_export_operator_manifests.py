"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os
import shutil
import tarfile
import tempfile
import zipfile

from atomic_reactor.constants import (PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY,
                                      OPERATOR_MANIFESTS_ARCHIVE)
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.util import (
    is_scratch_build,
    has_operator_appregistry_manifest,
    has_operator_bundle_manifest,
)
from atomic_reactor.utils.operator import OperatorManifest
from docker.errors import APIError
from platform import machine

MANIFESTS_DIR_NAME = 'manifests'
IMG_MANIFESTS_PATH = os.path.join('/', MANIFESTS_DIR_NAME)


class ExportOperatorManifestsPlugin(PostBuildPlugin):
    """
    Export operator manifest files

    Fetch and archive operator manifest files from image so they can be
    uploaded to koji.
    """

    key = PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, operator_manifests_extract_platform=None, platform=None):
        super(ExportOperatorManifestsPlugin, self).__init__(tasker, workflow)

        self.operator_manifests_extract_platform = operator_manifests_extract_platform
        if platform:
            self.platform = platform
        else:
            self.platform = machine()

    def should_run(self):
        """
        Check if the plugin should run or skip execution.

        :return: bool, False if plugin should skip execution
        """
        if self.is_in_orchestrator():
            self.log.warning("%s plugin set to run on orchestrator. Skipping", self.key)
            return False
        if self.operator_manifests_extract_platform != self.platform:
            self.log.info("Only platform [%s] will upload operators metadata. Skipping",
                          self.operator_manifests_extract_platform)
            return False
        if is_scratch_build(self.workflow):
            self.log.info("Scratch build. Skipping")
            return False
        if not (
            has_operator_bundle_manifest(self.workflow) or
            has_operator_appregistry_manifest(self.workflow)
        ):
            self.log.info("Operator manifests label not set in Dockerfile. Skipping")
            return False
        return True

    def run(self):
        """
        Run the plugin.

        This plugin extracts the operator manifest files from an image, saves
        them as a zip archive, and returns its path

        :return: str, path to operator manifests zip file
        """
        if not self.should_run():
            return

        manifests_archive_dir = tempfile.mkdtemp()
        image = self.workflow.image
        # As in flatpak_create_oci, we specify command to prevent possible docker daemon errors.
        container_dict = self.tasker.create_container(image, command=['/bin/bash'])
        container_id = container_dict['Id']
        try:
            bits, _ = self.tasker.get_archive(container_id,
                                              IMG_MANIFESTS_PATH)
        except APIError as ex:
            msg = ('Could not extract operator manifest files. '
                   'Is there a %s path in the image?' % (IMG_MANIFESTS_PATH))
            self.log.debug('Error while trying to extract %s from image: %s',
                           IMG_MANIFESTS_PATH, ex)
            self.log.error(msg)
            raise RuntimeError('%s %s' % (msg, ex)) from ex

        except Exception as ex:
            raise RuntimeError('%s' % ex) from ex

        finally:
            try:
                self.tasker.remove_container(container_id)
            except Exception as ex:
                self.log.warning('Failed to remove container %s: %s', container_id, ex)

        with tempfile.NamedTemporaryFile() as extracted_file:
            for chunk in bits:
                extracted_file.write(chunk)
            extracted_file.flush()
            tar_archive = tarfile.TarFile(extracted_file.name)

        tar_archive.extractall(manifests_archive_dir)
        manifests_path = os.path.join(manifests_archive_dir, MANIFESTS_DIR_NAME)

        if has_operator_bundle_manifest(self.workflow):
            self._verify_csv(manifests_path)

        manifests_zipfile_path = os.path.join(manifests_archive_dir, OPERATOR_MANIFESTS_ARCHIVE)
        with zipfile.ZipFile(manifests_zipfile_path, 'w') as archive:
            for root, _, files in os.walk(manifests_path):
                for f in files:
                    filedir = os.path.relpath(root, manifests_path)
                    filepath = os.path.join(filedir, f)
                    archive.write(os.path.join(root, f), filepath, zipfile.ZIP_DEFLATED)
            manifest_files = archive.namelist()
            if not manifest_files:
                self.log.error('Empty operator manifests directory')
                raise RuntimeError('Empty operator manifests directory')
            self.log.debug("Archiving operator manifests: %s", manifest_files)

        shutil.rmtree(manifests_path)

        return manifests_zipfile_path

    def _verify_csv(self, manifests_path):
        """Verify the CSV file from the built image

        :param str manifests_path:
        :raises: ValueError if more than one CSV files are found, or the single
            CSV is different from the one in the repo (compared by hash digest).
        """
        try:
            image_csv = OperatorManifest.from_directory(manifests_path).csv
        except ValueError as e:
            raise ValueError(f'Operator manifests check in built image failed: {e}') from e

        repo_csv = OperatorManifest.from_directory(self.workflow.source.manifests_dir).csv

        if image_csv.checksum != repo_csv.checksum:
            image_csv_filename = os.path.basename(image_csv.path)
            repo_csv_filename = os.path.basename(repo_csv.path)
            raise ValueError(
                f'The CSV file {image_csv_filename} included in the built image '
                f'and the original pinned CSV {repo_csv_filename} in dist-git '
                f'repo have different content.'
            )
