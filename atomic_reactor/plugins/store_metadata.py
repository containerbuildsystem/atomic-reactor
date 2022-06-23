"""
Copyright (c) 2015-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import json

from osbs.exceptions import OsbsResponseException

from atomic_reactor.plugins.fetch_sources import PLUGIN_FETCH_SOURCES_KEY
from atomic_reactor.constants import PLUGIN_VERIFY_MEDIA_KEY, SCRATCH_FROM
from atomic_reactor.plugin import Plugin
from atomic_reactor.util import get_manifest_digests


class StoreMetadataPlugin(Plugin):
    key = "store_metadata"
    is_allowed_to_fail = False

    def __init__(self, workflow):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        """
        # call parent constructor
        super(StoreMetadataPlugin, self).__init__(workflow)
        self.source_build = PLUGIN_FETCH_SOURCES_KEY in self.workflow.data.plugins_results

    def get_digests(self):
        """
        Returns a map of repositories to digests
        """

        digests = {}  # repository -> digest
        registry = self.workflow.conf.registry

        for image in self.workflow.data.tag_conf.images:
            image_digests = get_manifest_digests(image, registry['uri'], registry['insecure'],
                                                 registry.get('secret', None))
            if image_digests:
                digests[image.to_str()] = image_digests

        return digests

    def get_pullspecs(self, digests):
        # v2 registry digests
        pullspecs = []

        for image in self.workflow.data.tag_conf.images:
            image_str = image.to_str()
            if image_str in digests:
                digest = digests[image_str]
                for digest_version in digest.content_type:
                    if digest_version not in digest:
                        continue
                    pullspecs.append({
                        "registry": image.registry,
                        "repository": image.to_str(registry=False, tag=False),
                        "tag": image.tag,
                        "digest": digest[digest_version],
                        "version": digest_version
                    })

        return pullspecs

    def get_plugin_metadata(self):
        wf_data = self.workflow.data
        return {
            "errors": wf_data.plugins_errors,
            "timestamps": wf_data.plugins_timestamps,
            "durations": wf_data.plugins_durations,
        }

    def get_filesystem_metadata(self):
        data = {}
        try:
            data = self.workflow.fs_watcher.get_usage_data()
            self.log.debug("filesystem metadata: %s", data)
        except Exception:
            self.log.exception("Error getting filesystem stats")

        return data

    def set_koji_task_annotations_whitelist(self, annotations):
        """Whitelist annotations to be included in koji task output

        Allow annotations whose names are listed in task_annotations_whitelist
        koji's configuration to be included in the build_annotations.json file,
        which will be attached in the koji task output.
        """
        koji_config = self.workflow.conf.koji
        whitelist = koji_config.get('task_annotations_whitelist')
        if whitelist:
            annotations['koji_task_annotations_whitelist'] = json.dumps(whitelist)

    def _update_annotations(self, annotations, updates):
        if updates:
            updates = {key: json.dumps(value) for key, value in updates.items()}
            annotations.update(updates)

    def apply_plugin_annotations(self, annotations):
        self._update_annotations(annotations, self.workflow.data.annotations)

    def run(self):
        pipeline_run_name = self.workflow.pipeline_run_name
        self.log.info("pipelineRun name = %s", pipeline_run_name)

        wf_data = self.workflow.data

        if not self.source_build:
            try:
                commit_id = self.workflow.source.commit_id
            except AttributeError:
                commit_id = ""

            base_image = wf_data.dockerfile_images.original_base_image
            if base_image is not None and not wf_data.dockerfile_images.base_from_scratch:
                base_image_name = base_image
            else:
                base_image_name = ""

            parent_images_strings = self.workflow.parent_images_to_str()
            if wf_data.dockerfile_images.base_from_scratch:
                parent_images_strings[SCRATCH_FROM] = SCRATCH_FROM

            try:
                dockerfile = self.workflow.build_dir.any_platform.dockerfile_with_parent_env(
                    self.workflow.imageutil.base_image_inspect()
                )
                dockerfile_contents = dockerfile.content
            except FileNotFoundError:
                dockerfile_contents = ""

        annotations = {
            'digests': json.dumps(self.get_pullspecs(self.get_digests())),
            'plugins-metadata': json.dumps(self.get_plugin_metadata()),
            'filesystem': json.dumps(self.get_filesystem_metadata()),
        }

        if not self.source_build:
            annotations['dockerfile'] = dockerfile_contents
            annotations['commit_id'] = commit_id
            annotations['base-image-name'] = base_image_name
            annotations['parent_images'] = json.dumps(parent_images_strings)

        media_types = []

        media_results = wf_data.plugins_results.get(PLUGIN_VERIFY_MEDIA_KEY)

        if media_results:
            media_types += media_results

        if media_types:
            annotations['media-types'] = json.dumps(sorted(list(set(media_types))))

        self.apply_plugin_annotations(annotations)
        self.set_koji_task_annotations_whitelist(annotations)

        try:
            self.workflow.osbs.update_annotations_on_build(pipeline_run_name, annotations)
        except OsbsResponseException:
            self.log.debug("annotations: %r", annotations)
            raise

        return {"annotations": annotations}
