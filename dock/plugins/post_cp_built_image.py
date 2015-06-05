"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Copies exported squashed built image ($tmpdir/image.tar)
into specified directory under /run/secrets/
Usage:
{
    'name': 'cp_built_image',
    'args': {'dest_dir': 'custom_directory'}
}

"""


import os
import shutil
from dock.plugin import PostBuildPlugin


__all__ = ('CopyBuiltImagePlugin', )

DEFAULT_SECRETS = '/run/secrets/'
DEFAULT_DEST_DIR = 'built_images/'

class CopyBuiltImagePlugin(PostBuildPlugin):
    key = "cp_built_image"

    def __init__(self, tasker, workflow, secrets=None, dest_dir=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param secrets: mainly for testing, to be able to create dest_dir somewhere else than in /run/secrets/
        :param dest_dir: this directory will be created in 'secrets' (/run/secrets/ by default)
                         and the built image will be copied into it
        """
        # call parent constructor
        super(CopyBuiltImagePlugin, self).__init__(tasker, workflow)
        if secrets is None:
            secrets = DEFAULT_SECRETS
        self.secrets = secrets
        if dest_dir is None:
            dest_dir = DEFAULT_DEST_DIR
        self.dest_dir = os.path.join(self.secrets, dest_dir)

    def run(self):
        source_path = self.workflow.exported_squashed_image.get("path")
        self.log.info("Copying exported built image %s into %s", source_path, self.dest_dir)
        if not source_path or not os.path.isfile(source_path):
            self.log.error("%s is not a file.", source_path)
            return

        if not os.path.isdir(self.secrets):
            self.log.error("%s doesn't exist.", self.secrets)
            return

        if not os.path.isdir(self.dest_dir):
            try:
                os.mkdir(self.dest_dir)
                self.log.info("Creating %s", self.dest_dir)
            except (IOError, OSError) as ex:
                self.log.error("Couldn't create %s: %s", self.dest_dir,  repr(ex))
                raise

        try:
            shutil.copy2(source_path, self.dest_dir)
        except (IOError, OSError) as ex:
            self.log.error("Couldn't copy %s into %s: %s", source_path, self.dest_dir, repr(ex))
            raise

        fname = os.path.basename(source_path)
        if os.path.isfile(os.path.join(self.dest_dir, fname)):
            self.log.debug("CopyBuiltImagePlugin.run() success")
        else:
            self.log.error("CopyBuiltImagePlugin.run() unknown error")
