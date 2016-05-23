"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals

import json
import os
import docker

from osbs.api import OSBS
from osbs.conf import Configuration
from osbs.exceptions import OsbsResponseException

from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.plugins.pre_return_dockerfile import CpDockerfilePlugin
from atomic_reactor.plugins.pre_pyrpkg_fetch_artefacts import DistgitFetchArtefactsPlugin
from atomic_reactor.plugins.exit_koji_promote import KojiPromotePlugin
from atomic_reactor.util import get_build_json


class StoreMetadataInOSv3Plugin(ExitPlugin):
    key = "store_metadata_in_osv3"

    def __init__(self, tasker, workflow, url, verify_ssl=True, use_auth=True):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param url: str, URL to OSv3 instance
        :param use_auth: bool, initiate authentication with openshift?
        """
        # call parent constructor
        super(StoreMetadataInOSv3Plugin, self).__init__(tasker, workflow)
        self.url = url
        self.verify_ssl = verify_ssl
        self.use_auth = use_auth

    def get_result(self, result):
        if isinstance(result, Exception):
            result = ''

        return result

    def get_pre_result(self, key):
        return self.get_result(self.workflow.prebuild_results.get(key, ''))

    def get_exit_result(self, key):
        return self.get_result(self.workflow.exit_results.get(key, ''))

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

    def get_repositories(self):
        # usually repositories formed from NVR labels
        # these should be used for pulling and layering
        primary_repositories = []
        for registry in self.workflow.push_conf.all_registries:
            for image in self.workflow.tag_conf.primary_images:
                registry_image = image.copy()
                registry_image.registry = registry.uri
                primary_repositories.append(registry_image.to_str())

        # unique unpredictable repositories
        unique_repositories = []
        for registry in self.workflow.push_conf.all_registries:
            for image in self.workflow.tag_conf.unique_images:
                registry_image = image.copy()
                registry_image.registry = registry.uri
                unique_repositories.append(registry_image.to_str())

        return {
            "primary": primary_repositories,
            "unique": unique_repositories,
        }

    def get_pullspecs(self, digests):
        if self.workflow.push_conf.pulp_registries:
            # If pulp was used, only report pulp repositories
            registries = self.workflow.push_conf.pulp_registries
        else:
            # Otherwise report all the images we pushed
            registries = self.workflow.push_conf.all_registries

        # v2 registry digests
        pullspecs = []
        for registry in registries:
            for image in self.workflow.tag_conf.images:
                if image.to_str() in digests:
                    pullspecs.append({
                        "registry": registry.uri,
                        "repository": image.to_str(registry=False, tag=False),
                        "tag": image.tag or 'latest',
                        "digest": digests[image.to_str()]
                    })
        return pullspecs

    def get_plugin_metadata(self):
        return {
            "errors": self.workflow.plugins_errors,
            "timestamps": self.workflow.plugins_timestamps,
            "durations": self.workflow.plugins_durations,
        }

    def make_labels(self):
        labels = {}

        koji_build_id = self.get_exit_result(KojiPromotePlugin.key)
        if koji_build_id:
            labels["koji-build-id"] = str(koji_build_id)

        return labels

    def run(self):
        metadata = get_build_json().get("metadata", {})

        try:
            build_id = metadata["name"]
        except KeyError:
            self.log.error("malformed build json")
            return
        self.log.info("build id = %s", build_id)

        # initial setup will use host based auth: apache will be set to accept everything
        # from specific IP and will set specific X-Remote-User for such requests
        # FIXME: remove `openshift_uri` once osbs-client is released
        osbs_conf = Configuration(conf_file=None, openshift_uri=self.url, openshift_url=self.url,
                                  use_auth=self.use_auth, verify_ssl=self.verify_ssl,
                                  namespace=metadata.get('namespace', None))
        osbs = OSBS(osbs_conf, osbs_conf)

        try:
            commit_id = self.workflow.source.commit_id
        except AttributeError:
            commit_id = ""

        try:
            base_image_id = self.workflow.base_image_inspect['Id']
        except docker.errors.NotFound:
            base_image_id = ""

        annotations = {
            "dockerfile": self.get_pre_result(CpDockerfilePlugin.key),
            "artefacts": self.get_pre_result(DistgitFetchArtefactsPlugin.key),

            # We no longer store the 'docker build' logs as an annotation
            "logs": '',

            # We no longer store the rpm packages as an annotation
            "rpm-packages": '',

            "repositories": json.dumps(self.get_repositories()),
            "commit_id": commit_id,
            "base-image-id": base_image_id,
            "base-image-name": self.workflow.builder.base_image.to_str(),
            "image-id": self.workflow.builder.image_id or '',
            "digests": json.dumps(self.get_pullspecs(self.get_digests())),
            "plugins-metadata": json.dumps(self.get_plugin_metadata())
        }

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
        try:
            osbs.set_annotations_on_build(build_id, annotations)
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
