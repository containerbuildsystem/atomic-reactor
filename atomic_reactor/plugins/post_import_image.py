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
from atomic_reactor.util import ImageName


class ImportImagePlugin(PostBuildPlugin):
    """
    Import image tags from external docker registry into OpenShift.
    """

    key = "import_image"
    can_fail = False

    def __init__(self, tasker, workflow, url, verify_ssl=True, use_auth=True):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param url: str, URL to OSv3 instance
        :param verify_ssl: bool, verify SSL certificate?
        :param use_auth: bool, initiate authentication with openshift?
        """
        # call parent constructor
        super(ImportImagePlugin, self).__init__(tasker, workflow)
        self.url = url
        self.verify_ssl = verify_ssl
        self.use_auth = use_auth

    def run(self):
        try:
            build_json = json.loads(os.environ["BUILD"])
        except KeyError:
            self.log.error("No $BUILD env variable. "
                           "Probably not running in build container.")
            raise

        osbs_conf = Configuration(conf_file=None, openshift_uri=self.url,
                                  use_auth=self.use_auth,
                                  verify_ssl=self.verify_ssl)
        osbs = OSBS(osbs_conf, osbs_conf)

        metadata = build_json.get("metadata", {})
        kwargs = {}
        if 'namespace' in metadata:
            kwargs['namespace'] = metadata['namespace']

        labels = metadata.get("labels", {})
        try:
            imagestream = labels["imagestream"]
        except KeyError:
            self.log.error("No imagestream label set for this Build")
            raise

        self.log.info("Importing tags for %s", imagestream)
        osbs.import_image(imagestream, **kwargs)
