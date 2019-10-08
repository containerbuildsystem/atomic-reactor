"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import print_function, unicode_literals, absolute_import

import tempfile

from atomic_reactor.build import BuildResult
from atomic_reactor.constants import PLUGIN_BUILD_SOURCE_CONTAINER_KEY
from atomic_reactor.plugin import BuildStepPlugin
from atomic_reactor.plugins.pre_reactor_config import get_value


class SourceContainerBuilderPlugin(BuildStepPlugin):
    """
    Build source container image using
    https://github.com/containers/BuildSourceImage

    Image https://quay.io/repository/ctrs/bsi should be pushed to image stream
    on OCP instance and image name must be specified in config
    option `source_builder_image`
    """

    key = PLUGIN_BUILD_SOURCE_CONTAINER_KEY

    def get_builder_image(self):
        source_containers_conf = get_value(self.workflow, 'source_containers', {})
        return source_containers_conf.get('source_builder_image')

    def run(self):
        """Build image inside current environment.
        It's expected this may run within (privileged) docker container.

        Returns:
            BuildResult
        """
        source_data_dir = tempfile.mkdtemp()  # TODO: from pre_* plugin
        # TODO fail when source dir is empty

        image_output_dir = tempfile.mkdtemp()
        image = self.get_builder_image()
        if not image:
            raise RuntimeError(
                'Cannot build source containers, builder image is not '
                'specified in configuration')

        srpms_path = '/data/'
        output_path = '/output/'

        volume_bindings = {
            source_data_dir: {
                'bind': srpms_path,
                'mode': 'ro',
            },
            image_output_dir: {
                'bind': output_path,
                'mode': 'rw,Z',
            }
        }

        command = '-d sourcedriver_rpm_dir -s {srpms_path} -o {output_path}'.format(
            srpms_path=srpms_path,
            output_path=output_path,
        )
        container_id = self.tasker.run(
            image,
            volume_bindings=volume_bindings,
            command=command
        )
        response = self.tasker.wait(container_id)
        output = self.tasker.logs(container_id)

        self.tasker.cleanup_containers(container_id)

        if response != 0:
            reason = (
                "Source container build failed with error code {}. "
                "See build logs for details".format(response)
            )
            return BuildResult(logs=output, fail_reason=reason)

        return BuildResult(
            logs=output,
            oci_image_path=image_output_dir,
            skip_layer_squash=True
        )
