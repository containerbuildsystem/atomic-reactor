"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.constants import PLUGIN_REMOVE_WORKER_METADATA_KEY
from osbs.exceptions import OsbsResponseException


def defer_removal(workflow, cf_map, osbs):
    key = RemoveWorkerMetadataPlugin.key
    workspace = workflow.plugin_workspace.setdefault(key, {})
    workspace.setdefault('cf_maps_to_remove', set())
    workspace['cf_maps_to_remove'].add((cf_map, osbs))


class RemoveWorkerMetadataPlugin(ExitPlugin):
    """
    Remove worker metadata for each platform.
    """

    key = PLUGIN_REMOVE_WORKER_METADATA_KEY

    def run(self):
        """
        Run the plugin.
        """

        workspace = self.workflow.plugin_workspace.get(self.key, {})
        cf_maps_to_remove = workspace.get('cf_maps_to_remove', [])
        for cm_key, osbs in cf_maps_to_remove:
            try:
                osbs.delete_config_map(cm_key)
                self.log.debug("ConfigMap %s deleted", cm_key)
            except OsbsResponseException as ex:
                self.log.warning("Failed to delete ConfigMap %s: %s", cm_key, ex.message)
