"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import json
import os
from osbs.core import Openshift
from dock.util import ImageName

try:
    # py2
    from urlparse import urljoin
except Exception:
    # py3
    from urllib.parse import urljoin

from dock.plugin import PostBuildPlugin
from dock.plugins.pre_return_dockerfile import CpDockerfilePlugin
from dock.plugins.pre_pyrpkg_fetch_artefacts import DistgitFetchArtefactsPlugin
from dock.plugins.post_rpmqa import PostBuildRPMqaPlugin


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

        api_url = urljoin(self.url, "/osapi/v1beta1/")
        oauth_url = urljoin(self.url, "/oauth/authorize")  # MUST NOT END WITH SLASH

        # initial setup will use host based auth: apache will be set to accept everything
        # from specific IP and will set specific X-Remote-User for such requests
        o = Openshift(api_url, oauth_url, None, use_auth=self.use_auth, verify_ssl=self.verify_ssl)

        primary_repositories = []
        for registry_uri in self.workflow.tag_and_push_conf.registries:
            registry_conf = self.workflow.tag_and_push_conf[registry_uri]
            try:
                image_names = registry_conf['image_names']
            except KeyError:
                self.log.error("Registry '%s' doesn't have any image names, skipping...", registry_uri)
                continue
            for image in image_names:
                image_name = ImageName.parse(image)
                if image_name.registry:
                    assert image_name.registry == registry_uri
                image_name.registry = registry_uri
                primary_repositories.append(image_name.to_str())

        unique_repositories = []
        target_image = self.workflow.builder.image.copy()
        for registry in self.workflow.target_registries:
            target_image.registry = registry
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
            "tar_metadata": {
                "size": self.workflow.exported_squashed_image.get("size"),
                "md5sum": self.workflow.exported_squashed_image.get("md5sum"),
                "sha256sum": self.workflow.exported_squashed_image.get("sha256sum"),
            }
        }
        o.set_annotations_on_build(build_id, labels)
