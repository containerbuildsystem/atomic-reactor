"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import print_function, unicode_literals, absolute_import

import os
import subprocess
import tempfile

from atomic_reactor.build import BuildResult
from atomic_reactor.constants import PLUGIN_SOURCE_CONTAINER_KEY, PLUGIN_FETCH_SOURCES_KEY
from atomic_reactor.plugin import BuildStepPlugin


class SourceContainerPlugin(BuildStepPlugin):
    """
    Build source container image using
    https://github.com/containers/BuildSourceImage
    """

    key = PLUGIN_SOURCE_CONTAINER_KEY

    def run(self):
        """Build image inside current environment.

        Returns:
            BuildResult
        """
        fetch_sources_result = self.workflow.prebuild_results.get(PLUGIN_FETCH_SOURCES_KEY, {})
        source_data_dir = fetch_sources_result.get('image_sources_dir')
        if not source_data_dir or not os.path.isdir(source_data_dir):
            err_msg = "No SRPMs directory '{}' available".format(source_data_dir)
            self.log.error(err_msg)
            return BuildResult(logs=err_msg, fail_reason=err_msg)

        if not os.listdir(source_data_dir):
            self.log.warning("SRPMs directory '%s' is empty", source_data_dir)

        image_output_dir = tempfile.mkdtemp()

        cmd = ['bsi',
               '-d',
               'sourcedriver_rpm_dir',
               '-s',
               '{}'.format(source_data_dir),
               '-o',
               '{}'.format(image_output_dir)]

        try:
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            self.log.error("BSI failed with output:\n%s", e.output)
            return BuildResult(logs=e.output, fail_reason='BSI utility failed build source image')

        self.log.debug("Build log:\n%s\n", output)

        return BuildResult(
            logs=output,
            oci_image_path=image_output_dir,
            skip_layer_squash=True
        )
