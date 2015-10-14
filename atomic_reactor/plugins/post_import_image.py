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
from osbs.exceptions import OsbsResponseException

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.util import ImageName


class ImportImagePlugin(PostBuildPlugin):
    """
    Import image tags from external docker registry into Origin,
    creating an ImageStream if one does not already exist.
    """

    key = "import_image"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, imagestream, docker_image_repo,
                 url, build_json_dir, verify_ssl=True, use_auth=True):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param imagestream: str, name of ImageStream
        :param docker_image_repo: str, image repository to import tags from
        :param url: str, URL to OSv3 instance
        :param build_json_dir: str, path to directory with input json
        :param verify_ssl: bool, verify SSL certificate?
        :param use_auth: bool, initiate authentication with openshift?
        """
        # call parent constructor
        super(ImportImagePlugin, self).__init__(tasker, workflow)
        self.imagestream = imagestream
        self.docker_image_repo = docker_image_repo
        self.url = url
        self.verify_ssl = verify_ssl
        self.use_auth = use_auth
        self.build_json_dir = build_json_dir

    def run(self):
        try:
            build_json = json.loads(os.environ["BUILD"])
        except KeyError:
            self.log.error("No $BUILD env variable. "
                           "Probably not running in build container.")
            raise

        metadata = build_json.get("metadata", {})
        kwargs = {}
        if 'namespace' in metadata:
            kwargs['namespace'] = metadata['namespace']

        # FIXME: remove `openshift_uri` once osbs-client is released
        osbs_conf = Configuration(openshift_uri=self.url,
                                  openshift_url=self.url,
                                  use_auth=self.use_auth,
                                  verify_ssl=self.verify_ssl,
                                  build_json_dir=self.build_json_dir)
        osbs = OSBS(osbs_conf, osbs_conf)

        try:
            osbs.get_image_stream(self.imagestream, **kwargs)
        except OsbsResponseException:
            self.log.info("Creating ImageStream %s for %s", self.imagestream,
                          self.docker_image_repo)

            # Tags are imported automatically on creation
            osbs.create_image_stream(self.imagestream, self.docker_image_repo,
                                     **kwargs)
        else:
            self.log.info("Importing tags for %s", self.imagestream)
            osbs.import_image(self.imagestream, **kwargs)
