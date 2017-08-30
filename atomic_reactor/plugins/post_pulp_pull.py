"""Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Pull built image from Crane to discover its image ID.

After squashing our image, the squashed image 'docker save' form will
have an image ID that is correct for its v2 schema 2
representation. However since Pulp does not yet support v2 schema 2,
we will need to remove that local image and re-pull it from Crane to
discover the image ID Docker will give it.
"""

from __future__ import unicode_literals

from atomic_reactor.constants import PLUGIN_PULP_PUSH_KEY, PLUGIN_PULP_SYNC_KEY
from atomic_reactor.plugin import PostBuildPlugin, ExitPlugin
from atomic_reactor.plugins.exit_remove_built_image import defer_removal
from atomic_reactor.util import get_manifest_digests
import requests
from time import time, sleep


class CraneTimeoutError(Exception):
    """The expected image did not appear in the required time"""
    pass


# Note: We use multiple inheritance here only to make it explicit that
# this plugin needs to act as both an exit plugin (since arrangement
# version 4) and as a post-build plugin (arrangement version < 4). In
# fact, ExitPlugin is a subclass of PostBuildPlugin.
class PulpPullPlugin(ExitPlugin, PostBuildPlugin):
    key = 'pulp_pull'
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow,
                 timeout=600, retry_delay=30,
                 insecure=False, secret=None,
                 expect_v2schema2=False):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param timeout: int, maximum number of seconds to wait
        :param retry_delay: int, seconds between pull attempts
        :param insecure: bool, allow non-https pull if true
        :param secret: str, path to secret
        :param expect_v2schema2: bool, require Pulp to return a schema 2 digest and
                                       retry until it does
        """
        # call parent constructor
        super(PulpPullPlugin, self).__init__(tasker, workflow)
        self.timeout = timeout
        self.retry_delay = retry_delay
        self.insecure = insecure
        self.secret = secret
        self.expect_v2schema2 = expect_v2schema2

    def retry_if_not_found(self, func, *args, **kwargs):
        start = time()

        while True:
            try:
                digests = func(*args, **kwargs)
            except requests.exceptions.HTTPError as ex:
                # Retry for 404 not-found because we assume Crane has
                # not spotted the new Pulp content yet. For all other
                # errors, give up.
                if ex.response.status_code != requests.codes.not_found:
                    raise
            else:
                if not self.expect_v2schema2 or digests.v2:
                    return digests
                elif self.expect_v2schema2:
                    self.log.warn("Expected schema 2 manifest, but only schema 1 found")

            if time() - start > self.timeout:
                raise CraneTimeoutError("{} seconds exceeded"
                                        .format(self.timeout))

            self.log.info("not found; will try again in %ss", self.retry_delay)
            sleep(self.retry_delay)

    def run(self):
        # Only run if the build was successful
        if self.workflow.build_process_failed:
            self.log.info("Not running for failed build")
            return None, []

        # Work out the name of the image to pull
        assert self.workflow.tag_conf.unique_images  # must be set
        image = self.workflow.tag_conf.unique_images[0]

        assert self.workflow.push_conf.pulp_registries  # must be configured
        registry = self.workflow.push_conf.pulp_registries[0]

        pullspec = image.copy()
        pullspec.registry = registry.uri  # the image on Crane

        media_types = []
        for plugin in self.workflow.postbuild_plugins_conf:
            if plugin['name'] == PLUGIN_PULP_SYNC_KEY:
                media_types.append('application/vnd.docker.distribution.manifest.v1+json')
            if plugin['name'] == PLUGIN_PULP_PUSH_KEY:
                media_types.append('application/json')

        # We only expect to find a v2 digest from Crane if the
        # pulp_sync plugin was used. If we do find a v2 digest, there
        # is no need to pull the image.
        if registry.server_side_sync:
            digests = self.retry_if_not_found(get_manifest_digests,
                                              pullspec, registry.uri,
                                              self.insecure, self.secret,
                                              require_digest=False)
            if digests:
                if digests.v2_list:
                    self.log.info("Manifest list found")
                    media_types.append('application/vnd.docker.distribution.manifest.list.v2+json')
                if digests.v2:
                    self.log.info("V2 schema 2 digest found, returning %s",
                                  self.workflow.builder.image_id)
                    media_types.append('application/vnd.docker.distribution.manifest.v2+json')
                    # No need to pull the image to work out the image ID as
                    # we already know it.
                    return self.workflow.builder.image_id, sorted(media_types)
            else:
                self.log.info("No digests were found")

        # Pull the image from Crane to find out the image ID for the
        # v2 schema 1 manifest (which we have not seen before).
        name = self.tasker.pull_image(pullspec, insecure=self.insecure)

        # Inspect it
        metadata = self.tasker.inspect_image(name)

        defer_removal(self.workflow, name)

        # Adjust our idea of the image ID
        image_id = metadata['Id']
        self.log.debug("image ID changed from %s to %s",
                       self.workflow.builder.image_id,
                       image_id)
        self.workflow.builder.image_id = image_id

        return image_id, sorted(media_types)
