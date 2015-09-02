"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pre build plugin which changes FROM instruction
"""
import fileinput
import re
import sys
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import ImageName


class ChangeFromPlugin(PreBuildPlugin):
    key = "change_from_in_dockerfile"

    def __init__(self, tasker, workflow, base_image=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param base_image: str, change base image to ID of this image
        """
        # call parent constructor
        super(ChangeFromPlugin, self).__init__(tasker, workflow)
        self.base_image = ImageName.parse(base_image) if base_image else None

    def run(self):
        """
        run the plugin
        """
        base_image = self.base_image or self.workflow.builder.base_image
        try:
            base_image_id = self.workflow.base_image_inspect['Id']
        except KeyError:
            self.log.error("Id is missing in inspection: '%s'", self.workflow.base_image_inspect)
            raise
        self.log.debug("using base image '%s', id '%s'", base_image, base_image_id)
        for line in fileinput.input(self.workflow.builder.df_path, inplace=1):
            re_match = re.match(r"^FROM .+$", line)
            if re_match:
                new_from = "FROM %s" % base_image_id
                sys.stdout.write(new_from + '\n')
                self.log.info("changed FROM: '%s' -> '%s'", re_match.group(0), new_from)
            else:
                sys.stdout.write(line)
