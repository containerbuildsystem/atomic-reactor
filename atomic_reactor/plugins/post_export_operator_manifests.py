"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import

import os
import shutil
import tarfile
import tempfile
import zipfile

from atomic_reactor.constants import (PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY,
                                      OPERATOR_MANIFESTS_ARCHIVE)
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.util import df_parser, is_scratch_build, get_platforms
from docker.errors import APIError
from osbs.utils import Labels
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

    def has_operator_manifest(self):
        """
        Check if Dockerfile sets the operator manifest label

        :return: bool
        """
        dockerfile = df_parser(self.workflow.builder.df_path, workflow=self.workflow)
        labels = Labels(dockerfile.labels)
        try:
            _, operator_label = labels.get_name_and_value(Labels.LABEL_TYPE_OPERATOR_MANIFESTS)
        except KeyError:
            operator_label = 'false'
        return operator_label.lower() == 'true'

    def is_orchestrator(self):
        """
        Check if the plugin is running in orchestrator.

        :return: bool
        """
        if get_platforms(self.workflow):
            return True
        return False

    def should_run(self):
        """
        Check if the plugin should run or skip execution.

        :return: bool, False if plugin should skip execution
        """
        if self.is_orchestrator():
            self.log.warning("%s plugin set to run on orchestrator. Skipping", self.key)
            return False
        if self.operator_manifests_extract_platform != self.platform:
            self.log.info("Only platform [%s] will upload operators metadata. Skipping",
                          self.operator_manifests_extract_platform)
            return False
        if is_scratch_build():
            self.log.info("Scratch build. Skipping")
            return False
        if not self.has_operator_manifest():
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
        container_dict = self.tasker.d.create_container(image, command=['/bin/bash'])
        container_id = container_dict['Id']
        try:
            bits, stat = self.tasker.d.get_archive(container_id,
                                                   IMG_MANIFESTS_PATH)
        except APIError as ex:
            msg = ('Could not extract operator manifest files. '
                   'Is there a %s path in the image?' % (IMG_MANIFESTS_PATH))
            self.log.debug('Error while trying to extract %s from image: %s',
                           IMG_MANIFESTS_PATH, ex)
            self.log.error(msg)
            raise RuntimeError('%s %s' % (msg, ex))

        except Exception as ex:
            raise RuntimeError('%s' % ex)

        finally:
            try:
                self.tasker.d.remove_container(container_id)
            except Exception as ex:
                self.log.warning('Failed to remove container %s: %s' % (container_id, ex))

        with tempfile.NamedTemporaryFile() as extracted_file:
            for chunk in bits:
                extracted_file.write(chunk)
            extracted_file.flush()
            tar_archive = tarfile.TarFile(extracted_file.name)

        tar_archive.extractall(manifests_archive_dir)
        manifests_path = os.path.join(manifests_archive_dir, MANIFESTS_DIR_NAME)

        manifests_zipfile_path = os.path.join(manifests_archive_dir, OPERATOR_MANIFESTS_ARCHIVE)
        with zipfile.ZipFile(manifests_zipfile_path, 'w') as archive:
            for root, _, files in os.walk(manifests_path):
                for f in files:
                    filedir = os.path.relpath(root, manifests_path)
                    filepath = os.path.join(filedir, f)
                    archive.write(os.path.join(root, f), filepath)
            manifest_files = archive.namelist()
            if not manifest_files:
                self.log.error('Empty operator manifests directory')
                raise RuntimeError('Empty operator manifests directory')
            self.log.debug("Archiving operator manifests: %s", manifest_files)

        shutil.rmtree(manifests_path)

        return manifests_zipfile_path
