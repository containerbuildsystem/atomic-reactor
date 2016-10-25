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

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.plugins.exit_remove_built_image import defer_removal
from docker.errors import NotFound
from time import time, sleep


class CraneTimeoutError(Exception):
    """The expected image did not appear in the required time"""
    pass


class PulpPullPlugin(PostBuildPlugin):
    key = 'pulp_pull'
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, timeout=600, retry_delay=30):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param timeout: int, maximum number of seconds to wait
        :param retry_delay: int, seconds between pull attempts
        """
        # call parent constructor
        super(PulpPullPlugin, self).__init__(tasker, workflow)
        self.timeout = timeout
        self.retry_delay = retry_delay

    def run(self):
        start = time()

        # Work out the name of the image to pull
        assert self.workflow.tag_conf.unique_images  # must be set
        image = self.workflow.tag_conf.unique_images[0]

        assert self.workflow.push_conf.pulp_registries  # must be configured
        registry = self.workflow.push_conf.pulp_registries[0]

        pullspec = image.copy()
        pullspec.registry = registry.uri  # the image on Crane

        while True:
            # Pull the image from Crane
            name = self.tasker.pull_image(pullspec)

            # Inspect it
            try:
                metadata = self.tasker.inspect_image(name)
            except NotFound:
                if time() - start > self.timeout:
                    raise CraneTimeoutError("{} seconds exceeded"
                                            .format(self.timeout))

                self.log.info("will try again in %ss", self.retry_delay)
                sleep(self.retry_delay)
                continue

            defer_removal(self.workflow, name)
            break

        # Adjust our idea of the image ID
        image_id = metadata['Id']
        self.log.debug("image ID changed from %s to %s",
                       self.workflow.builder.image_id,
                       image_id)
        self.workflow.builder.image_id = image_id

        return image_id
