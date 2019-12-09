"""
Copyright (c) 2017, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import absolute_import

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.plugins.build_orchestrate_build import get_worker_build_info
from atomic_reactor.constants import PLUGIN_FETCH_WORKER_METADATA_KEY
from atomic_reactor.util import get_platform_config, BadConfigMapError


class FetchWorkerMetadataPlugin(PostBuildPlugin):
    """
    Fetch worker metadata from each platform and return a dict of
    each platform's metadata.

    """

    key = PLUGIN_FETCH_WORKER_METADATA_KEY
    is_allowed_to_fail = False

    def get_platform_metadata(self, platform, build_annotations):
        """
        Return the metadata for the given platform.
        """
        # retrieve all the workspace data
        cm_key, cm_frag_key = get_platform_config(platform, build_annotations)

        build_info = get_worker_build_info(self.workflow, platform)
        osbs = build_info.osbs
        try:
            cm_data = osbs.get_config_map(cm_key)
        except Exception:
            self.log.error("Failed to get ConfigMap for platform %s",
                           platform)
            raise

        metadata = cm_data.get_data_by_key(cm_frag_key)
        return metadata

    def run(self):
        """
        Run the plugin.
        """

        metadatas = {}

        # get all the build annotations and labels from the orchestrator
        build_result = self.workflow.build_result

        annotations = build_result.annotations
        worker_builds = annotations['worker-builds']

        for platform, build_annotations in worker_builds.items():
            try:
                metadata = self.get_platform_metadata(platform,
                                                      build_annotations)
            except BadConfigMapError:
                continue  # should we just fail here instead?
            except Exception:
                self.log.error("Failed to get metadata for platform %s",
                               platform)
                raise

            metadatas[platform] = metadata

        return metadatas
