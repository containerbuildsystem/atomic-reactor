"""
Copyright (c) 2017, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.plugins.build_orchestrate_build import get_worker_build_info
from atomic_reactor.constants import PLUGIN_FETCH_WORKER_METADATA_KEY
from atomic_reactor.plugins.exit_remove_worker_metadata import defer_removal


class BadConfigMapError(Exception):
    """
    Build annotation does not indicate a valid ConfigMap.
    """


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
        build_info = get_worker_build_info(self.workflow, platform)
        osbs = build_info.osbs

        kind = "configmap/"
        cmlen = len(kind)
        cm_key_tmp = build_annotations['metadata_fragment']
        cm_frag_key = build_annotations['metadata_fragment_key']

        if not cm_key_tmp or not cm_frag_key or cm_key_tmp[:cmlen] != kind:
            msg = "Bad ConfigMap annotations for platform {}".format(platform)
            self.log.warning(msg)
            raise BadConfigMapError(msg)

        # use the key to get the configmap data and then use the
        # fragment_key to get the build metadata inside the configmap data
        # save the worker_build metadata
        cm_key = cm_key_tmp[cmlen:]
        try:
            cm_data = osbs.get_config_map(cm_key)
        except Exception:
            self.log.error("Failed to get ConfigMap for platform %s",
                           platform)
            raise

        metadata = cm_data.get_data_by_key(cm_frag_key)
        defer_removal(self.workflow, cm_key, osbs)
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
