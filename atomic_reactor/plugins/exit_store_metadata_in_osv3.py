"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals

import json
import os

from osbs.api import OSBS
from osbs.conf import Configuration

from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.plugins.pre_return_dockerfile import CpDockerfilePlugin
from atomic_reactor.plugins.pre_pyrpkg_fetch_artefacts import DistgitFetchArtefactsPlugin
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin


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

    def get_post_result(self, key):
        return self.get_result(self.workflow.postbuild_results.get(key, ''))

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

    def get_digests(self):
        # v2 registry digests
        digests = []
        for registry in self.workflow.push_conf.docker_registries:
            for image in self.workflow.tag_conf.images:
                if image.to_str() in registry.digests:
                    digests.append({
                        "registry": registry.uri,
                        "repository": image.to_str(registry=False, tag=False),
                        "tag": image.tag or 'latest',
                        "digest": registry.digests[image.to_str()]
                    })
        return digests

    def run(self):
        try:
            build_json = json.loads(os.environ["BUILD"])
        except KeyError:
            self.log.error("No $BUILD env variable. Probably not running in build container.")
            return

        kwargs = {}
        metadata = build_json.get("metadata", {})
        if 'namespace' in metadata:
            kwargs['namespace'] = metadata['namespace']

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
                                  use_auth=self.use_auth, verify_ssl=self.verify_ssl)
        osbs = OSBS(osbs_conf, osbs_conf)

        try:
            commit_id = self.workflow.source.lg.commit_id
        except AttributeError:
            commit_id = ""

        labels = {
            "dockerfile": self.get_pre_result(CpDockerfilePlugin.key),
            "artefacts": self.get_pre_result(DistgitFetchArtefactsPlugin.key),
            "logs": "\n".join(self.workflow.build_logs),
            "rpm-packages": "\n".join(self.get_post_result(PostBuildRPMqaPlugin.key)),
            "repositories": json.dumps(self.get_repositories()),
            "commit_id": commit_id,
            "base-image-id": self.workflow.base_image_inspect['Id'],
            "base-image-name": self.workflow.builder.base_image.to_str(),
            "image-id": self.workflow.builder.image_id,
            "digests": json.dumps(self.get_digests()),
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
            labels["tar_metadata"] = json.dumps({
                "size": tar_size,
                "md5sum": tar_md5sum,
                "sha256sum": tar_sha256sum,
                "filename": os.path.basename(tar_path),
            })
        osbs.set_annotations_on_build(build_id, labels, **kwargs)
        return labels
