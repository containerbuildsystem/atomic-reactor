"""
Copyright (c) 2018 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import print_function, unicode_literals

import subprocess
import time
from six import PY2
import os

from atomic_reactor.util import get_exported_image_metadata
from atomic_reactor.plugin import BuildStepPlugin
from atomic_reactor.build import BuildResult
from atomic_reactor.constants import CONTAINER_IMAGEBUILDER_BUILD_METHOD
from atomic_reactor.constants import EXPORTED_SQUASHED_IMAGE_NAME, IMAGE_TYPE_DOCKER_ARCHIVE


def sixdecode(data):
    return data.decode() if PY2 else data


class ImagebuilderPlugin(BuildStepPlugin):
    """
    Build image using imagebuilder https://github.com/openshift/imagebuilder
    This requires the imagebuilder executable binary to be in $PATH.
    """

    key = CONTAINER_IMAGEBUILDER_BUILD_METHOD

    def run(self):
        """
        Build image inside current environment using imagebuilder;
        It's expected this may run within (privileged) docker container.

        Returns:
            BuildResult
        """
        builder = self.workflow.builder

        image = builder.image.to_str()
        # TODO: directly invoke go imagebuilder library in shared object via python module
        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if not PY2:
            kwargs['encoding'] = 'utf-8'
        ib_process = subprocess.Popen(['imagebuilder', '-t', image, builder.df_dir], **kwargs)

        self.log.debug('imagebuilder build has begun; waiting for it to finish')
        (output, last_error) = ([], None)
        while True:
            poll = ib_process.poll()
            out = sixdecode(ib_process.stdout.readline())
            if out:
                self.log.info(out.strip())
                output.append(out)
            err = sixdecode(ib_process.stderr.readline())
            if err:
                self.log.error(err.strip())
                output.append(err)  # include stderr with stdout
                last_error = err    # while noting the final line
            if out == '' and err == '':
                if poll is not None:
                    break
                time.sleep(0.1)  # don't busy-wait when there's no output

        if ib_process.returncode != 0:
            # imagebuilder uses stderr for normal output too; so in the case of an apparent
            # failure, single out the last line to include in the failure summary.
            err = last_error or "<imagebuilder had bad exit code but no error output>"
            return BuildResult(
                logs=output,
                fail_reason="image build failed (rc={}): {}".format(ib_process.returncode, err),
            )

        image_id = builder.get_built_image_info()['Id']
        if ':' not in image_id:
            # Older versions of the daemon do not include the prefix
            image_id = 'sha256:{}'.format(image_id)

        # since we need no squash, export the image for local operations like squash would have
        self.log.info("fetching image %s from docker", image)
        output_path = os.path.join(self.workflow.source.workdir, EXPORTED_SQUASHED_IMAGE_NAME)
        with open(output_path, "w") as image_file:
            image_file.write(self.tasker.d.get_image(image).data)
        img_metadata = get_exported_image_metadata(output_path, IMAGE_TYPE_DOCKER_ARCHIVE)
        self.workflow.exported_image_sequence.append(img_metadata)

        return BuildResult(logs=output, image_id=image_id, skip_layer_squash=True)
