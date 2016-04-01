"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


It returns the dockerfile itself and therefore displays it in results.
"""
from __future__ import unicode_literals
import os

from atomic_reactor.constants import EXPORTED_SQUASHED_IMAGE_NAME
from atomic_reactor.plugin import PrePublishPlugin
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
            "remove_former_image": false,
            "dont_load": false
          }
        }
      }
    ```

    The `tag` argument specifes the tag under which the new squashed image will
    be registered. The `from_layer` argument specifies from which layer we want
    to squash. `remove_former_image` is an optional boolean argument which specifies
    if former, unsquashed image should be removed.

    Of course it's possible to override it at runtime, like this: `--substitute prepublish_plugins.squash.tag=image:squashed
      --substitute prepublish_plugins.squash.from_layer=asdasd2332`.
    """

    key = "squash"
    # Fail the build in case of squashing error
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, tag=None, from_base=True, from_layer=None,
                 remove_former_image=True, dont_load=False):
        """
        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param from_base: bool, squash from base-image layer, on by default
        :param from_layer: layer from we will squash - if specified, takes precedence over from_base
        :param tag: str, new name of the image - by default use the former one
        :param remove_former_image: bool, remove unsquashed image?
        :param dont_load: if `False` (default), squashed image is loaded into Docker *and* saved
            to `$tmpdir/image.tar`; if `True`, squashed image is only saved as a file
        """
        super(PrePublishSquashPlugin, self).__init__(tasker, workflow)
        self.image = self.workflow.builder.image_id
        self.tag = tag or str(self.workflow.builder.image)
        self.from_layer = from_layer
        if from_base and from_layer is None:
            try:
                base_image_id = self.workflow.base_image_inspect['Id']
            except KeyError:
                self.log.error("Missing Id in inspection: '%s'", self.workflow.base_image_inspect)
                raise
            self.log.info("will squash from base-image: '%s'", base_image_id)
            self.from_layer = base_image_id
        self.remove_former_image = remove_former_image
        self.dont_load = dont_load

    def run(self):
        metadata = {"path":
                    os.path.join(self.workflow.source.workdir, EXPORTED_SQUASHED_IMAGE_NAME)}

        if self.dont_load:
            # squash the image, don't load it back to docker
            Squash(log=self.log, image=self.image, from_layer=self.from_layer,
                   tag=self.tag, output_path=metadata["path"], load_image=False).run()
        else:
            # squash the image and output both tarfile and Docker engine image
            new_id = Squash(log=self.log, image=self.image, from_layer=self.from_layer,
                            tag=self.tag, output_path=metadata["path"], load_image=True).run()
            self.workflow.builder.image_id = new_id

        metadata.update(get_exported_image_metadata(metadata["path"]))
        self.workflow.exported_image_sequence.append(metadata)

        if self.remove_former_image:
            self.tasker.remove_image(self.image)
