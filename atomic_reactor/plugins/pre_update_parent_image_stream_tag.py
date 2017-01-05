"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import get_build_json
from osbs.api import OSBS
from osbs.conf import Configuration
from osbs.exceptions import OsbsResponseException

import requests


class UpdateParentImageStreamTagPlugin(PreBuildPlugin):
    """Update the parent image stream tag if needed

    This plugin fetches the ImageStreamTag that corresponding to
    this image's FROM instruction in Dockerfile.
    If found, its importPolicy is checked and updated if needed.
    If not found, a new ImageStreamTag is created.

    Example configuration:

    {
      "name": "update_parent_image_stream_tag",
      "args": {
        "image_stream_tag": "fedora:25",
        "openshift_url": "https://localhost:8443"
      }
    }
    """

    key = "update_parent_image_stream_tag"
    is_allowed_to_fail = False  # We really want to stop the process

    def __init__(self, tasker, workflow, image_stream_tag, openshift_url,
                 build_json_dir, use_auth=True, verify_ssl=True,
                 scheduled=False):
        """Constructor.

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param image_stream_tag: str: ImageStreamTag for this image's parent image
        :param openshift_url: str, URL to OSv3 instance
        :param build_json_dir: str, path to directory with input json
        :param use_auth: bool, initiate authentication with OSv3?
        :param verify_ssl: bool, verify SSL certificate of OSv3 instance?
        :param scheduled: ImageStreamTag's importPolicy is set to scheduled
        """
        super(UpdateParentImageStreamTagPlugin, self).__init__(tasker, workflow)
        self.image_stream_tag = image_stream_tag
        self.openshift_url = openshift_url
        self.verify_ssl = verify_ssl
        self.use_auth = use_auth
        self.scheduled = scheduled
        self.build_json_dir = build_json_dir

    def _get_osbs(self):
        """Create and return an instance of OSBS."""
        namespace = get_build_json().get("metadata", {}).get('namespace')
        osbs_conf = Configuration(conf_file=None,
                                  openshift_url=self.openshift_url,
                                  use_auth=self.use_auth,
                                  verify_ssl=self.verify_ssl,
                                  build_json_dir=self.build_json_dir,
                                  namespace=namespace)
        return OSBS(osbs_conf, osbs_conf)

    def run(self):
        """Run the plugin."""
        osbs = self._get_osbs()

        image_stream_id, tag_name = self.image_stream_tag.split(':')
        try:
            image_stream = osbs.get_image_stream(image_stream_id).json()
        except OsbsResponseException as exc:
            if exc.status_code == requests.codes.not_found:
                self.log.warning(
                    'Parent image stream, %s, not found, skipping',
                    image_stream_id)
                return False

            raise

        changed = osbs.ensure_image_stream_tag(image_stream,
                                               tag_name,
                                               self.scheduled)
        self.log.info('Changed parent image stream tag? %s', changed)
        return changed
