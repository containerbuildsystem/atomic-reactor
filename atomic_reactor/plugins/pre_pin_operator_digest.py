"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import, print_function

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.constants import PLUGIN_PIN_OPERATOR_DIGESTS_KEY
from atomic_reactor.util import has_operator_bundle_manifest
from atomic_reactor.plugins.pre_reactor_config import get_operator_manifests
from atomic_reactor.plugins.build_orchestrate_build import override_build_kwarg


class PinOperatorDigestsPlugin(PreBuildPlugin):
    """
    Plugin runs for operator manifest bundle builds.

    When running in orchestrator:
    - finds container pullspecs in operator ClusterServiceVersion files
    - computes replacement pullspecs:
        - replaces tags with manifest list digests
        - replaces registries based on operator_manifests.registry_post_replace in r-c-m*

    When running in a worker:
    - receives replacement pullspec mapping computed by orchestrator
    - replaces pullspecs in ClusterServiceVersion files based on said mapping

    * reactor-config-map
    """

    key = PLUGIN_PIN_OPERATOR_DIGESTS_KEY
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, replacement_pullspecs=None):
        """
        Initialize pin_operator_digests plugin

        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param replacement_pullspecs: Dict[str, str], computed in orchestrator,
                                      provided to workers by osbs-client
        """
        super(PinOperatorDigestsPlugin, self).__init__(tasker, workflow)
        self.user_config = workflow.source.config.operator_manifest
        self.site_config = None  # Only relevant (and available) in orchestrator
        self.replacement_pullspecs = replacement_pullspecs

    def run(self):
        if self.should_run():
            if self.is_in_orchestrator():
                return self.run_in_orchestrator()
            else:
                return self.run_in_worker()

    def should_run(self):
        if has_operator_bundle_manifest(self.workflow):
            return True
        else:
            self.log.info("Not an operator manifest bundle build, skipping plugin")
            return False

    def run_in_orchestrator(self):
        try:
            self.site_config = get_operator_manifests(self.workflow)
        except KeyError:
            raise RuntimeError("operator_manifests configuration missing in reactor config map")
        override_build_kwarg(self.workflow, "operator_bundle_replacement_pullspecs", {})

    def run_in_worker(self):
        pass
