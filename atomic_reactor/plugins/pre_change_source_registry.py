"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pre build plugin which changes source registry
"""
from atomic_reactor.plugin import PreBuildPlugin


class ChangeSourceRegistryPlugin(PreBuildPlugin):
    key = "change_source_registry"

    def __init__(self, tasker, workflow, registry_uri, insecure_registry=False):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param registry_uri: str, URI of the registry
        :param insecure_registry: bool, is connection insecure (is it http only?)
                                  should SSL cert verification be ignored?
        """
        # call parent constructor
        super(ChangeSourceRegistryPlugin, self).__init__(tasker, workflow)
        self.registry_uri = registry_uri
        self.insecure_registry = insecure_registry

    def run(self):
        """
        run the plugin
        """
        self.log.debug("setting source registry to '%s', insecure = '%s'", self.registry_uri, self.insecure_registry)
        self.workflow.parent_registry = self.registry_uri
        self.workflow.parent_registry_insecure = self.insecure_registry
