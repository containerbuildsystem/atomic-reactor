"""
Copyright (c) 2019-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os
import shutil
import tempfile
import zipfile
from typing import Optional, TYPE_CHECKING

from osbs.utils import ImageName

from atomic_reactor.constants import (PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY,
                                      OPERATOR_MANIFESTS_ARCHIVE)
from atomic_reactor.plugin import Plugin
from atomic_reactor.util import (
    get_platforms,
    has_operator_appregistry_manifest,
    has_operator_bundle_manifest,
)
from atomic_reactor.utils.operator import OperatorCSV, OperatorManifest

if TYPE_CHECKING:
    from atomic_reactor.inner import DockerBuildWorkflow

MANIFESTS_DIR_NAME = 'manifests'
IMG_MANIFESTS_PATH = f'/{MANIFESTS_DIR_NAME}/'


class ExportOperatorManifestsPlugin(Plugin):
    """
    Export operator manifest files

    Fetch and archive operator manifest files from image, so they can be
    uploaded to koji.
    """

    key = PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY
    is_allowed_to_fail = False

    def __init__(self, workflow: "DockerBuildWorkflow"):
        super(ExportOperatorManifestsPlugin, self).__init__(workflow)

    def run(self) -> Optional[str]:
        """
        Run the plugin.

        This plugin extracts the operator manifest files from an image, saves
        them as a zip archive, and returns its path

        :return: str, path to operator manifests zip file
        """
        if not (
                has_operator_bundle_manifest(self.workflow) or
                has_operator_appregistry_manifest(self.workflow)
        ):
            self.log.info("Operator manifests label not set in Dockerfile. Skipping")
            return None

        platforms = get_platforms(self.workflow.data)
        image: ImageName = self.workflow.data.tag_conf.get_unique_images_with_platform(
            platforms[0])[0]
        tmp_dir = tempfile.mkdtemp(dir=self.workflow.build_dir.any_platform.path)
        manifests_dir = os.path.join(tmp_dir, MANIFESTS_DIR_NAME)
        os.mkdir(manifests_dir)

        self.workflow.imageutil.extract_file_from_image(image, IMG_MANIFESTS_PATH, manifests_dir)

        if has_operator_bundle_manifest(self.workflow):
            self._verify_csv(manifests_dir)

        manifests_zipfile_path = (self.workflow.build_dir.any_platform.path /
                                  OPERATOR_MANIFESTS_ARCHIVE)
        with zipfile.ZipFile(manifests_zipfile_path, 'w') as archive:
            for root, _, files in os.walk(manifests_dir):
                for f in files:
                    filedir = os.path.relpath(root, manifests_dir)
                    filepath = os.path.join(filedir, f)
                    archive.write(os.path.join(root, f), filepath, zipfile.ZIP_DEFLATED)
            manifest_files = archive.namelist()
            self.log.debug("Archiving operator manifests: %s", manifest_files)

        shutil.rmtree(tmp_dir)

        return str(manifests_zipfile_path)

    def _verify_csv(self, manifests_dir) -> None:
        """Verify the CSV file from the built image

        :param str manifests_dir:
        :raises: ValueError if more than one CSV files are found, or the single
            CSV is different from the one in the repo (compared by hash digest).
        """
        try:
            image_csv: OperatorCSV = OperatorManifest.from_directory(manifests_dir).csv
        except ValueError as e:
            raise ValueError(f'Operator manifests check in built image failed: {e}') from e

        repo_csv: OperatorCSV = OperatorManifest.from_directory(
            os.path.join(
                self.workflow.build_dir.any_platform.path,
                self.workflow.source.config.operator_manifests["manifests_dir"],
            )
        ).csv

        if image_csv.checksum != repo_csv.checksum:
            image_csv_filename = os.path.basename(image_csv.path)
            repo_csv_filename = os.path.basename(repo_csv.path)
            raise ValueError(
                f'The CSV file {image_csv_filename} included in the built image '
                f'and the original pinned CSV {repo_csv_filename} in dist-git '
                f'repo have different content.'
            )
