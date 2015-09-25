"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


This plugin copies dockerfile to provided path. Useful when building from
command line, or directly on host
"""
import shutil

from atomic_reactor.plugin import PreBuildPlugin


class CpDockerfilePlugin(PreBuildPlugin):
    key = "cp_dockerfile"

    def __init__(self, tasker, workflow, path):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param path: str, path where df should be copied
        :return:
        """
        # call parent constructor
        super(CpDockerfilePlugin, self).__init__(tasker, workflow)
        self.path = path

    def run(self):
        """
        each plugin has to implement this method -- it is used to run the plugin actually

        response from plugin is kept and used in json result response
        """
        try:
            shutil.copyfile(self.workflow.builder.df_path, self.path)
        except (IOError, OSError) as ex:
            msg = "Couldn't copy dockerfile: %r" % ex
            self.log.error(msg)
            return msg
        else:
            msg = "Dockerfile successfully copied to '%s'" % self.path
            self.log.info(msg)
            return msg
