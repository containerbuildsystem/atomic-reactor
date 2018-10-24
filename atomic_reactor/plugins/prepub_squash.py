"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


It returns the dockerfile itself and therefore displays it in results.
"""
from __future__ import unicode_literals
import os

from atomic_reactor.constants import EXPORTED_SQUASHED_IMAGE_NAME, IMAGE_TYPE_DOCKER_ARCHIVE
from atomic_reactor.plugin import PrePublishPlugin
from atomic_reactor.plugins.exit_remove_built_image import defer_removal
from atomic_reactor.util import get_exported_image_metadata
from docker_squash.squash import Squash

__all__ = ('PrePublishSquashPlugin', )


class PrePublishSquashPlugin(PrePublishPlugin):

    """
    This feature requires docker-squash package to be installed in version 1.0.0rc3
    or higher.

    Usage:

    A json build config file should be created with following content:

    ```
      "prepublish_plugins": [{
        "name": "squash",
          "args": {
            "tag": "SQUASH_TAG",
            "from_layer": "FROM_LAYER",
            "dont_load": false
          }
        }
      ]
    ```

    The `tag` argument specifes the tag under which the new squashed image will
    be registered. The `from_layer` argument specifies from which layer we want
    to squash.

    Of course it's possible to override it at runtime, like this: `--substitute
    prepublish_plugins.squash.tag=image:squashed
      --substitute prepublish_plugins.squash.from_layer=asdasd2332`.
    """

    key = "squash"
    # Fail the build in case of squashing error
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, tag=None, from_base=True, from_layer=None,
                 dont_load=False, save_archive=True):
        """
        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param from_base: bool, squash from base-image layer, on by default
        :param from_layer: layer from we will squash - if specified, takes precedence over from_base
        :param tag: str, new name of the image - by default use the former one
        :param dont_load: if `False` (default), squashed image is loaded back into Docker daemon;
            if `True`, squashed image is not loaded back into Docker
        :param save_archive: if `True` (default), squashed image is saved in an archive on the
            disk under the image.tar name; if `False`, archive is not generated
        """
        super(PrePublishSquashPlugin, self).__init__(tasker, workflow)
        self.image = self.workflow.builder.image_id
        self.tag = tag or str(self.workflow.builder.image)
        self.from_layer = from_layer
        if from_base and from_layer is None:
            try:
                base_image_id = self.workflow.builder.base_image_inspect['Id']
            except KeyError:
                self.log.error("Missing Id in inspection: '%s'",
                               self.workflow.builder.base_image_inspect)
                raise
            self.log.info("will squash from base-image: '%s'", base_image_id)
            self.from_layer = base_image_id
        self.dont_load = dont_load
        self.save_archive = save_archive

    def run(self):
        if self.workflow.build_result.skip_layer_squash:
            return  # enable build plugins to prevent unnecessary squashes
        if self.save_archive:
            output_path = os.path.join(self.workflow.source.workdir, EXPORTED_SQUASHED_IMAGE_NAME)
            metadata = {"path": output_path}
        else:
            output_path = None

        # Squash the image and output tarfile
        # If the parameter dont_load is set to True squashed image won't be
        # loaded in to Docker daemon. If it's set to False it will be loaded.
        new_id = Squash(log=self.log, image=self.image, from_layer=self.from_layer,
                        tag=self.tag, output_path=output_path, load_image=not self.dont_load).run()

        if ':' not in new_id:
            # Older versions of the daemon do not include the prefix
            new_id = 'sha256:{}'.format(new_id)

        if not self.dont_load:
            self.workflow.builder.image_id = new_id

        if self.save_archive:
            metadata.update(get_exported_image_metadata(output_path, IMAGE_TYPE_DOCKER_ARCHIVE))
            self.workflow.exported_image_sequence.append(metadata)
        defer_removal(self.workflow, self.image)
