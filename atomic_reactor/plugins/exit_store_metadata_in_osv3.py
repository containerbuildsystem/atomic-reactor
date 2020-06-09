"""
Copyright (c) 2015, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals, absolute_import

import json
import os

from osbs.exceptions import OsbsResponseException

from atomic_reactor.plugins.pre_reactor_config import get_openshift_session, get_koji
from atomic_reactor.plugins.pre_fetch_sources import PLUGIN_FETCH_SOURCES_KEY
from atomic_reactor.constants import (PLUGIN_KOJI_UPLOAD_PLUGIN_KEY,
                                      PLUGIN_VERIFY_MEDIA_KEY,
                                      PLUGIN_RESOLVE_REMOTE_SOURCE,
                                      SCRATCH_FROM)
from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.util import get_build_json


class StoreMetadataInOSv3Plugin(ExitPlugin):
    key = "store_metadata_in_osv3"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, url=None, verify_ssl=True, use_auth=True):
        """
        constructor

        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param url: str, URL to OSv3 instance
        :param use_auth: bool, initiate authentication with openshift?
        """
        # call parent constructor
        super(StoreMetadataInOSv3Plugin, self).__init__(tasker, workflow)
        self.openshift_fallback = {
            'url': url,
            'insecure': not verify_ssl,
            'auth': {'enable': use_auth}
        }
        self.source_build = PLUGIN_FETCH_SOURCES_KEY in self.workflow.prebuild_results

    def get_result(self, result):
        if isinstance(result, Exception):
            result = ''

        return result

    def get_pre_result(self, key):
        return self.get_result(self.workflow.prebuild_results.get(key, ''))

    def get_post_result(self, key):
        return self.get_result(self.workflow.postbuild_results.get(key, ''))

    def get_exit_result(self, key):
        return self.get_result(self.workflow.exit_results.get(key, ''))

    def get_config_map(self):
        annotations = self.get_post_result(PLUGIN_KOJI_UPLOAD_PLUGIN_KEY)
        if not annotations:
            return {}

        return annotations

    def get_digests(self):
        """
        Returns a map of repositories to digests
        """

        digests = {}  # repository -> digest
        for registry in self.workflow.push_conf.docker_registries:
            for image in self.workflow.tag_conf.images:
                image_str = image.to_str()
                if image_str in registry.digests:
                    digest = registry.digests[image_str]
                    digests[image.to_str(registry=False)] = digest

        return digests

    def _get_registries(self):
        """
        Return a list of registries that this build updated
        """
        return self.workflow.push_conf.all_registries

    def get_repositories(self):
        # usually repositories formed from NVR labels
        # these should be used for pulling and layering
        primary_repositories = []
        for registry in self._get_registries():
            for image in self.workflow.tag_conf.primary_images:
                registry_image = image.copy()
                registry_image.registry = registry.uri
                primary_repositories.append(registry_image.to_str())

        # unique unpredictable repositories
        unique_repositories = []
        for registry in self._get_registries():
            for image in self.workflow.tag_conf.unique_images:
                registry_image = image.copy()
                registry_image.registry = registry.uri
                unique_repositories.append(registry_image.to_str())

        # floating repositories
        # these should be used for pulling and layering
        floating_repositories = []
        for registry in self._get_registries():
            for image in self.workflow.tag_conf.floating_images:
                registry_image = image.copy()
                registry_image.registry = registry.uri
                floating_repositories.append(registry_image.to_str())
        return {
            "primary": primary_repositories,
            "unique": unique_repositories,
            "floating": floating_repositories,
        }

    def get_pullspecs(self, digests):
        # v2 registry digests
        pullspecs = []
        for registry in self._get_registries():
            for image in self.workflow.tag_conf.images:
                image_str = image.to_str()
                if image_str in digests:
                    digest = digests[image_str]
                    for digest_version in digest.content_type:
                        if digest_version not in digest:
                            continue
                        pullspecs.append({
                            "registry": registry.uri,
                            "repository": image.to_str(registry=False, tag=False),
                            "tag": image.tag,
                            "digest": digest[digest_version],
                            "version": digest_version
                        })

        return pullspecs

    def get_plugin_metadata(self):
        return {
            "errors": self.workflow.plugins_errors,
            "timestamps": self.workflow.plugins_timestamps,
            "durations": self.workflow.plugins_durations,
        }

    def get_filesystem_metadata(self):
        data = {}
        try:
            data = self.workflow.fs_watcher.get_usage_data()
            self.log.debug("filesystem metadata: %s", data)
        except Exception:
            self.log.exception("Error getting filesystem stats")

        return data

    def _update_labels(self, labels, updates):
        if updates:
            updates = {key: str(value) for key, value in updates.items()}
            labels.update(updates)

    def make_labels(self):
        labels = {}
        self._update_labels(labels, self.workflow.labels)
        self._update_labels(labels, self.workflow.build_result.labels)

        return labels

    def set_koji_task_annotations_whitelist(self, annotations):
        """Whitelist annotations to be included in koji task output

        Allow annotations whose names are listed in task_annotations_whitelist
        koji's configuration to be included in the build_annotations.json file,
        which will be attached in the koji task output.
        """
        koji_config = get_koji(self.workflow)
        whitelist = koji_config.get('task_annotations_whitelist')
        if whitelist:
            annotations['koji_task_annotations_whitelist'] = json.dumps(whitelist)

    def _update_annotations(self, annotations, updates):
        if updates:
            updates = {key: json.dumps(value) for key, value in updates.items()}
            annotations.update(updates)

    def apply_build_result_annotations(self, annotations):
        self._update_annotations(annotations, self.workflow.build_result.annotations)

    def apply_plugin_annotations(self, annotations):
        self._update_annotations(annotations, self.workflow.annotations)

    def apply_remote_source_annotations(self, annotations):
        try:
            rs_annotations = self.get_pre_result(PLUGIN_RESOLVE_REMOTE_SOURCE)['annotations']
        except (TypeError, KeyError):
            return
        annotations.update(rs_annotations)

    def run(self):
        metadata = get_build_json().get("metadata", {})

        try:
            build_id = metadata["name"]
        except KeyError:
            self.log.error("malformed build json")
            return
        self.log.info("build id = %s", build_id)
        osbs = get_openshift_session(self.workflow, self.openshift_fallback)

        if not self.source_build:
            try:
                commit_id = self.workflow.source.commit_id
            except AttributeError:
                commit_id = ""

            # for early flatpak failure before it creates Dockerfile and creates dockerfile_images
            if self.workflow.builder.dockerfile_images is None:
                base_image_name = ""
                base_image_id = ""
                parent_images_strings = {}
            else:
                base_image = self.workflow.builder.dockerfile_images.original_base_image
                if (base_image is not None and
                        not self.workflow.builder.dockerfile_images.base_from_scratch):
                    base_image_name = base_image
                    try:
                        base_image_id = self.workflow.builder.base_image_inspect['Id']
                    except KeyError:
                        base_image_id = ""
                else:
                    base_image_name = ""
                    base_image_id = ""

                parent_images_strings = self.workflow.builder.parent_images_to_str()
                if self.workflow.builder.dockerfile_images.base_from_scratch:
                    parent_images_strings[SCRATCH_FROM] = SCRATCH_FROM

            try:
                with open(self.workflow.builder.df_path) as f:
                    dockerfile_contents = f.read()
            except AttributeError:
                dockerfile_contents = ""

        annotations = {
            'repositories': json.dumps(self.get_repositories()),
            'digests': json.dumps(self.get_pullspecs(self.get_digests())),
            'plugins-metadata': json.dumps(self.get_plugin_metadata()),
            'filesystem': json.dumps(self.get_filesystem_metadata()),
        }

        if self.source_build:
            annotations['image-id'] = ''
            if self.workflow.koji_source_manifest:
                annotations['image-id'] = self.workflow.koji_source_manifest['config']['digest']
        else:
            annotations['dockerfile'] = dockerfile_contents
            annotations['commit_id'] = commit_id
            annotations['base-image-id'] = base_image_id
            annotations['base-image-name'] = base_image_name
            annotations['image-id'] = self.workflow.builder.image_id or ''
            annotations['parent_images'] = json.dumps(parent_images_strings)

        media_types = []

        media_results = self.workflow.exit_results.get(PLUGIN_VERIFY_MEDIA_KEY)
        if isinstance(media_results, Exception):
            media_results = None

        if media_results:
            media_types += media_results

        if media_types:
            annotations['media-types'] = json.dumps(sorted(list(set(media_types))))

        tar_path = tar_size = tar_md5sum = tar_sha256sum = None
        if len(self.workflow.exported_image_sequence) > 0:
            tar_path = self.workflow.exported_image_sequence[-1].get("path")
            tar_size = self.workflow.exported_image_sequence[-1].get("size")
            tar_md5sum = self.workflow.exported_image_sequence[-1].get("md5sum")
            tar_sha256sum = self.workflow.exported_image_sequence[-1].get("sha256sum")
        # looks like that openshift can't handle value being None (null in json)
        if tar_size is not None and tar_md5sum is not None and tar_sha256sum is not None and \
                tar_path is not None:
            annotations["tar_metadata"] = json.dumps({
                "size": tar_size,
                "md5sum": tar_md5sum,
                "sha256sum": tar_sha256sum,
                "filename": os.path.basename(tar_path),
            })

        self.apply_remote_source_annotations(annotations)

        annotations.update(self.get_config_map())

        self.apply_plugin_annotations(annotations)
        self.apply_build_result_annotations(annotations)
        self.set_koji_task_annotations_whitelist(annotations)

        try:
            osbs.update_annotations_on_build(build_id, annotations)
        except OsbsResponseException:
            self.log.debug("annotations: %r", annotations)
            raise

        labels = self.make_labels()
        if labels:
            try:
                osbs.update_labels_on_build(build_id, labels)
            except OsbsResponseException:
                self.log.debug("labels: %r", labels)
                raise

        return {"annotations": annotations, "labels": labels}
