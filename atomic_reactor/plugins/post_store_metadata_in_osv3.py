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

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.plugins.pre_return_dockerfile import CpDockerfilePlugin
from atomic_reactor.plugins.pre_pyrpkg_fetch_artefacts import DistgitFetchArtefactsPlugin
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin


class StoreMetadataInOSv3Plugin(PostBuildPlugin):
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

    def run(self):
        try:
            build_json = json.loads(os.environ["BUILD"])
        except KeyError:
            self.log.error("No $BUILD env variable. Probably not running in build container.")
            return
        try:
            build_id = build_json["metadata"]["name"]
        except KeyError:
            self.log.error("malformed build json")
            return
        self.log.info("build id = %s", build_id)

        # initial setup will use host based auth: apache will be set to accept everything
        # from specific IP and will set specific X-Remote-User for such requests
        osbs_conf = Configuration(conf_file=None, openshift_uri=self.url,
                                  use_auth=self.use_auth, verify_ssl=self.verify_ssl)
        osbs = OSBS(osbs_conf, osbs_conf)

        # usually repositories formed from NVR labels
        # these should be used for pulling and layering
        primary_repositories = []
        for registry in self.workflow.push_conf.all_registries:
            for image in self.workflow.tag_conf.images:
                registry_image = image.copy()
                registry_image.registry = registry.uri
                primary_repositories.append(registry_image.to_str())

        # unique unpredictable repositories
        unique_repositories = []
        target_image = self.workflow.builder.image.copy()
        for registry in self.workflow.push_conf.all_registries:
            target_image.registry = registry.uri
            unique_repositories.append(target_image.to_str())

        repositories = {
            "primary": primary_repositories,
            "unique": unique_repositories,
        }

        try:
            commit_id = self.workflow.source.lg.commit_id
        except AttributeError:
            commit_id = ""

        labels = {
            "dockerfile": self.workflow.prebuild_results.get(CpDockerfilePlugin.key, ""),
            "artefacts": self.workflow.prebuild_results.get(DistgitFetchArtefactsPlugin.key, ""),
            "logs": "\n".join(self.workflow.build_logs),
            "rpm-packages": "\n".join(self.workflow.postbuild_results.get(PostBuildRPMqaPlugin.key, "")),
            "repositories": json.dumps(repositories),
            "commit_id": commit_id,
        }

        tar_path = self.workflow.exported_squashed_image.get("path")
        tar_size = self.workflow.exported_squashed_image.get("size")
        tar_md5sum = self.workflow.exported_squashed_image.get("md5sum")
        tar_sha256sum = self.workflow.exported_squashed_image.get("sha256sum")
        # looks like that openshift can't handle value being None (null in json)
        if tar_size is not None and tar_md5sum is not None and tar_sha256sum is not None and \
                tar_path is not None:
            labels["tar_metadata"] = json.dumps({
                "size": tar_size,
                "md5sum": tar_md5sum,
                "sha256sum": tar_sha256sum,
                "filename": os.path.basename(tar_path),
            })
        osbs.set_annotations_on_build(build_id, labels)
