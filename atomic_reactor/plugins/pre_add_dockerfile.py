"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Include user-provided Dockerfile in the IMAGE_BUILD_INFO_DIR
(or other if provided) directory in the built image.
This is accomplished by appending an ADD command to it.
Name of the Dockerfile is changed to include N-V-R of the build.
N-V-R is specified either by nvr argument OR from
Name/Version/Release labels in Dockerfile.
If you run add_labels_in_dockerfile to add Name/Version/Release labels
you have to run it BEFORE this one.


Example configuration:
{
    'name': 'add_dockerfile',
    'args': {'nvr': 'rhel-server-docker-7.1-20'}
}

or

[{
   'name': 'add_labels_in_dockerfile',
   'args': {'labels': {'Name': 'jboss-eap-6-docker',
                       'Version': '6.4',
                       'Release': '77'}}
},
{
   'name': 'add_dockerfile'
}]

"""

import os
import shutil

from osbs.utils import Labels

from atomic_reactor.constants import DOCKERFILE_FILENAME, IMAGE_BUILD_INFO_DIR
from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugin import PreBuildPlugin


class AddDockerfilePlugin(PreBuildPlugin):
    key = "add_dockerfile"

    def __init__(self, workflow, nvr=None, destdir=IMAGE_BUILD_INFO_DIR,
                 use_final_dockerfile=False):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :param nvr: name-version-release, will be appended to Dockerfile-.
                    If not specified, try to get it from Name, Version, Release labels.
        :param destdir: directory in the image to put Dockerfile-N-V-R into
        :param use_final_dockerfile: bool, when set to True, uses final version of processed
                                     dockerfile,
                                     when set to False, uses Dockerfile from time when this plugin
                                     was executed
        """
        # call parent constructor
        super(AddDockerfilePlugin, self).__init__(workflow)

        self.use_final_dockerfile = use_final_dockerfile

        if nvr is None:
            nvr = self._nvr_from_dockerfile()

        self.df_name = f"{DOCKERFILE_FILENAME}-{nvr}"
        self.df_path = os.path.join(destdir, self.df_name)

    def _nvr_from_dockerfile(self) -> str:
        # any_platform: the N-V-R labels should be equal for all platforms
        dockerfile = self.workflow.build_dir.any_platform.dockerfile_with_parent_env(
            self.workflow.imageutil.base_image_inspect()
        )
        labels = Labels(dockerfile.labels)
        try:
            _, name = labels.get_name_and_value(Labels.LABEL_TYPE_NAME)
            _, version = labels.get_name_and_value(Labels.LABEL_TYPE_VERSION)
            _, release = labels.get_name_and_value(Labels.LABEL_TYPE_RELEASE)
        except KeyError as exc:
            raise ValueError(
                "Required name/version/release labels not found in Dockerfile"
            ) from exc
        nvr = f"{name}-{version}-{release}"
        return nvr.replace("/", "-")

    def add_dockerfile(self, build_dir: BuildDir) -> None:
        dockerfile = build_dir.dockerfile
        lines = dockerfile.lines

        if self.use_final_dockerfile:
            # when using final dockerfile, we should use DOCKERFILE_FILENAME
            add_line = f'ADD {DOCKERFILE_FILENAME} {self.df_path}\n'
        else:
            # otherwise we should copy current snapshot and use the copied version
            shutil.copy2(build_dir.dockerfile_path, build_dir.path / self.df_name)
            add_line = f'ADD {self.df_name} {self.df_path}\n'

        # put it before last instruction
        lines.insert(-1, add_line)

        dockerfile.lines = lines

        self.log.info("added %s for the %s build", self.df_path, build_dir.platform)

    def run(self):
        """Run the plugin."""
        self.workflow.build_dir.for_each_platform(self.add_dockerfile)
