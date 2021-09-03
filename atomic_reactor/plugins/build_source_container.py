"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os
import shutil
import subprocess
import tempfile

from atomic_reactor.build import BuildResult
from atomic_reactor.constants import (PLUGIN_SOURCE_CONTAINER_KEY, EXPORTED_SQUASHED_IMAGE_NAME,
                                      IMAGE_TYPE_DOCKER_ARCHIVE, PLUGIN_FETCH_SOURCES_KEY)
from atomic_reactor.plugin import BuildStepPlugin
from atomic_reactor.util import get_exported_image_metadata
from atomic_reactor.utils import retries


class SourceContainerPlugin(BuildStepPlugin):
    """
    Build source container image using
    https://github.com/containers/BuildSourceImage
    """

    key = PLUGIN_SOURCE_CONTAINER_KEY

    def export_image(self, image_output_dir):
        output_path = os.path.join(tempfile.mkdtemp(), EXPORTED_SQUASHED_IMAGE_NAME)

        cmd = ['skopeo', 'copy']
        source_img = 'oci:{}'.format(image_output_dir)
        dest_img = 'docker-archive:{}'.format(output_path)
        cmd += [source_img, dest_img]

        try:
            retries.run_cmd(cmd)
        except subprocess.CalledProcessError as e:
            self.log.error("failed to save docker-archive :\n%s", e.output)
            raise

        img_metadata = get_exported_image_metadata(output_path, IMAGE_TYPE_DOCKER_ARCHIVE)
        self.workflow.exported_image_sequence.append(img_metadata)

    def split_remote_sources_to_subdirs(self, remote_source_data_dir):
        """Splits remote source archives to subdirs"""
        sources_subdirs = []
        for count, archive in enumerate(os.listdir(remote_source_data_dir)):
            subdir = os.path.join(remote_source_data_dir, f"remote_source_{count}")
            if not os.path.exists(subdir):
                os.makedirs(subdir)

            old_path = os.path.join(remote_source_data_dir, archive)
            new_path = os.path.join(subdir, archive)

            shutil.move(old_path, new_path)
            sources_subdirs.append(subdir)
        return sources_subdirs

    def run(self):
        """Build image inside current environment.

        Returns:
            BuildResult
        """
        fetch_sources_result = self.workflow.prebuild_results.get(PLUGIN_FETCH_SOURCES_KEY, {})
        source_data_dir = fetch_sources_result.get('image_sources_dir')
        remote_source_data_dir = fetch_sources_result.get('remote_sources_dir')
        maven_source_data_dir = fetch_sources_result.get('maven_sources_dir')

        source_exists = source_data_dir and os.path.isdir(source_data_dir)
        remote_source_exists = remote_source_data_dir and os.path.isdir(remote_source_data_dir)
        maven_source_exists = maven_source_data_dir and os.path.isdir(maven_source_data_dir)

        if not any([source_exists, remote_source_exists, maven_source_exists]):
            err_msg = "No SRPMs directory '{}' available".format(source_data_dir)
            err_msg += "\nNo Remote source directory '{}' available".format(remote_source_data_dir)
            err_msg += "\nNo Maven source directory '{}' available".format(maven_source_data_dir)
            self.log.error(err_msg)
            return BuildResult(logs=err_msg, fail_reason=err_msg)

        if source_exists and not os.listdir(source_data_dir):
            self.log.warning("SRPMs directory '%s' is empty", source_data_dir)
        if remote_source_exists and not os.listdir(remote_source_data_dir):
            self.log.warning("Remote source directory '%s' is empty", remote_source_data_dir)
        if maven_source_exists and not os.listdir(maven_source_data_dir):
            self.log.warning("Maven source directory '%s' is empty", maven_source_data_dir)

        image_output_dir = tempfile.mkdtemp()
        cmd = ['bsi', '-d']
        drivers = set()

        if source_exists:
            drivers.add('sourcedriver_rpm_dir')
            cmd.append('-s')
            cmd.append('{}'.format(source_data_dir))

        if remote_source_exists:
            sources_subdirs = self.split_remote_sources_to_subdirs(remote_source_data_dir)
            drivers.add('sourcedriver_extra_src_dir')

            for source_subdir in sources_subdirs:
                cmd.append('-e')
                cmd.append(source_subdir)

        if maven_source_exists:
            drivers.add('sourcedriver_extra_src_dir')
            for maven_source_subdir in os.listdir(maven_source_data_dir):
                cmd.append('-e')
                cmd.append('{}'.format(os.path.join(maven_source_data_dir, maven_source_subdir)))

        driver_str = ','.join(drivers)
        cmd.insert(2, driver_str)
        cmd.append('-o')
        cmd.append('{}'.format(image_output_dir))

        try:
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            self.log.error("BSI failed with output:\n%s", e.output)
            return BuildResult(logs=e.output, fail_reason='BSI utility failed build source image')

        self.log.debug("Build log:\n%s\n", output)

        self.export_image(image_output_dir)

        return BuildResult(
            logs=output,
            oci_image_path=image_output_dir,
            skip_layer_squash=True
        )
