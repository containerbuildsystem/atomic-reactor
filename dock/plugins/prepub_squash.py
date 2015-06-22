"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


It returns the dockerfile itself and therefore displays it in results.
"""
from __future__ import unicode_literals
import os

from os.path import getsize, isfile
from hashlib import md5, sha256
from dock.constants import EXPORTED_SQUASHED_IMAGE_NAME
from dock.plugin import PrePublishPlugin
from docker_scripts.squash import Squash

__all__ = ('PrePublishSquashPlugin', )


class PrePublishSquashPlugin(PrePublishPlugin):

    """
    This feature requires docker-scripts package to be installed in version 0.3.2
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
    can_fail = False

    def __init__(self, tasker, workflow, tag=None, from_layer=None, remove_former_image=True,
                 dont_load=False):
        """
        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param from_layer: The layer from we will squash - by default it'll be the first layer
        :param tag: str, new name of the image - by default use the former one
        :param remove_former_image: bool, remove unsquashed image?
        :param dont_load: bool, don't load squashed image into Docker, place it to `$tmpdir/image.tar` instead
        """
        super(PrePublishSquashPlugin, self).__init__(tasker, workflow)
        self.image = self.workflow.builder.image_id
        self.from_layer = from_layer
        self.tag = tag or str(self.workflow.builder.image)
        self.remove_former_image = remove_former_image
        self.dont_load = dont_load

    def _get_tarball_metadata(self):
        self.log.info("Getting exported squashed tarball metadata")
        path = self.workflow.exported_squashed_image.get("path")
        if not path or not isfile(path):
            self.log.error("%s is not a file.", path)
            return

        self.workflow.exported_squashed_image["size"] = getsize(path)
        self.log.debug("size: %d bytes", self.workflow.exported_squashed_image["size"])
        m = md5()
        s = sha256()
        blocksize = 65536
        with open(path, mode='rb') as f:
            buf = f.read(blocksize)
            while len(buf) > 0:
                m.update(buf)
                s.update(buf)
                buf = f.read(blocksize)
        self.workflow.exported_squashed_image["md5sum"] = m.hexdigest()
        self.log.debug("md5sum: %s", self.workflow.exported_squashed_image["md5sum"])
        self.workflow.exported_squashed_image["sha256sum"] = s.hexdigest()
        self.log.debug("sha256sum: %s", self.workflow.exported_squashed_image["sha256sum"])

    def run(self):
        if self.dont_load:
            self.workflow.exported_squashed_image["path"] = \
                os.path.join(self.workflow.source.workdir, EXPORTED_SQUASHED_IMAGE_NAME)
            # squash the image, don't load it back to docker
            Squash(log=self.log, image=self.image, from_layer=self.from_layer,
                   tag=self.tag, output_path=self.workflow.exported_squashed_image.get("path")).run()
        else:
            # squash the image and load it back to engine
            new_id = Squash(log=self.log, image=self.image, from_layer=self.from_layer,
                            tag=self.tag).run()
            self.workflow.builder.image_id = new_id
        self._get_tarball_metadata()
        if self.remove_former_image:
            self.tasker.remove_image(self.image)
