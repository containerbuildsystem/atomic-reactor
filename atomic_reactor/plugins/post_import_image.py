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
from atomic_reactor.util import get_build_json, ImageName


class ImportImagePlugin(PostBuildPlugin):
    """
    Import image tags from external docker registry into Origin,
    creating an ImageStream if one does not already exist.
    """

    key = "import_image"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, imagestream, docker_image_repo,
                 url, build_json_dir, verify_ssl=True, use_auth=True,
                 insecure_registry=None):
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
        imagestream = None
        try:
            imagestream = osbs.get_image_stream(self.imagestream)
        except OsbsResponseException:
            if self.insecure_registry is not None:
                kwargs['insecure_registry'] = self.insecure_registry

            self.log.info("Creating ImageStream %s for %s", self.imagestream,
                          self.docker_image_repo)

            imagestream = osbs.create_image_stream(self.imagestream,
                                                   self.docker_image_repo,
                                                   **kwargs)
        self.log.info("Importing new tags for %s", self.imagestream)

        primaries = None
        try:
            primaries = self.workflow.build_result.annotations['repositories']['primary']
        except (TypeError, KeyError):
            self.log.exception('Unable to read primary repositories annotations')

        if not primaries:
            raise RuntimeError('Could not find primary images in workflow')

        failures = False
        for s in primaries:
            tag_image_name = ImageName.parse(s)
            tag = tag_image_name.tag
            try:
                osbs.ensure_image_stream_tag(imagestream.json(), tag)
                self.log.info("Imported ImageStreamTag: (%s)", tag)
            except OsbsResponseException:
                failures = True
                self.log.info("Could not import ImageStreamTag: (%s)", tag)
        if failures:
            raise RuntimeError("Failed to import ImageStreamTag(s). Check logs")

        osbs.import_image(self.imagestream)
