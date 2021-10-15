"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.constants import PLUGIN_REMOVE_WORKER_METADATA_KEY
from osbs.exceptions import OsbsResponseException
from atomic_reactor.util import get_platform_config, BadConfigMapError
from atomic_reactor.plugins.build_orchestrate_build import get_worker_build_info


class RemoveWorkerMetadataPlugin(ExitPlugin):
    """
    Remove worker metadata for each platform.
    """

    key = PLUGIN_REMOVE_WORKER_METADATA_KEY

    def run(self):
        """
        Run the plugin.
        """
        build_result = self.workflow.build_result

        if not build_result.annotations:
            self.log.info("No build annotations found, skipping plugin")
            return

        worker_builds = build_result.annotations.get('worker-builds', {})

        for platform, build_annotations in worker_builds.items():
            try:
                if ('metadata_fragment' not in build_annotations or
                        'metadata_fragment_key' not in build_annotations):
                    continue

                cm_key, _ = get_platform_config(platform, build_annotations)
            except BadConfigMapError:
                continue
            # OSBS2 TBD: `get_worker_build_info` is imported from build_orchestrate_build
            build_info = get_worker_build_info(self.workflow, platform)
            osbs = build_info.osbs

            try:
                osbs.delete_config_map(cm_key)
                self.log.debug("ConfigMap %s on platform %s deleted", cm_key, platform)
            except OsbsResponseException as ex:
                self.log.warning("Failed to delete ConfigMap %s on platform %s: %s",
                                 cm_key, platform, ex)
