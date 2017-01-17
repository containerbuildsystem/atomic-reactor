"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from time import sleep

from osbs.api import OSBS
from osbs.conf import Configuration
from osbs.exceptions import OsbsResponseException

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.util import get_build_json


class ImportImagePlugin(PostBuildPlugin):
    """
    Import image tags from external docker registry into Origin,
    creating an ImageStream if one does not already exist.
    """

    key = "import_image"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, imagestream, docker_image_repo,
                 url, build_json_dir, verify_ssl=True, use_auth=True,
                 insecure_registry=None, retry_delay=30, import_attempts=3):
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
        :param insecure_registry: bool, whether the Docker registry uses
               plain HTTP
        :param retry_delay: int, number of seconds to delay before retrying
        :param import_attempts: int, number of times it will be retried to
               import image; at least 1.
        """
        # call parent constructor
        super(ImportImagePlugin, self).__init__(tasker, workflow)
        self.imagestream = imagestream
        self.docker_image_repo = docker_image_repo
        self.url = url
        self.build_json_dir = build_json_dir
        self.verify_ssl = verify_ssl
        self.use_auth = use_auth
        self.insecure_registry = insecure_registry
        self.retry_delay = retry_delay

        if import_attempts < 1:
            msg = "import_attempts is %d, should be at least 1"
            raise ValueError(msg % import_attempts)

        self.import_attempts = import_attempts

    def run(self):
        metadata = get_build_json().get("metadata", {})
        kwargs = {}

        # FIXME: remove `openshift_uri` once osbs-client is released
        osbs_conf = Configuration(conf_file=None,
                                  openshift_uri=self.url,
                                  openshift_url=self.url,
                                  use_auth=self.use_auth,
                                  verify_ssl=self.verify_ssl,
                                  build_json_dir=self.build_json_dir,
                                  namespace=metadata.get('namespace', None))
        osbs = OSBS(osbs_conf, osbs_conf)

        try:
            osbs.get_image_stream(self.imagestream)
        except OsbsResponseException:
            if self.insecure_registry is not None:
                kwargs['insecure_registry'] = self.insecure_registry

            self.log.info("Creating ImageStream %s for %s", self.imagestream,
                          self.docker_image_repo)

            # Tags are imported automatically on creation
            osbs.create_image_stream(self.imagestream, self.docker_image_repo,
                                     **kwargs)
        else:
            self.log.info("Importing new tags for %s", self.imagestream)

            attempts = 0
            while not osbs.import_image(self.imagestream):
                attempts += 1

                if attempts >= self.import_attempts:
                    msg = "Failed to import new tags for %s"
                    raise RuntimeError(msg % self.imagestream)

                self.log.info("no new tags, will retry after %d seconds (%d/%d)",
                              self.retry_delay, attempts, self.import_attempts)
                sleep(self.retry_delay)
