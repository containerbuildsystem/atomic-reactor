"""
Copyright (c) 2015, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from osbs.exceptions import OsbsResponseException
from osbs.utils import retry_on_conflict

from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.util import get_floating_images, is_scratch_build
from atomic_reactor.config import get_openshift_session


class ImportImagePlugin(ExitPlugin):
    """
    Import image tags from external docker registry into Origin,
    creating an ImageStream if one does not already exist.
    """

    key = 'import_image'
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, imagestream=None):
        """
        constructor

        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param imagestream: str, name of ImageStream
        """
        # call parent constructor
        super(ImportImagePlugin, self).__init__(tasker, workflow)
        self.imagestream_name = imagestream

        self.insecure_registry = self.workflow.conf.source_registry['insecure']

        self.osbs = None
        self.imagestream = None
        self.floating_images = None
        self.docker_image_repo = None

    def run(self):
        # Only run if the build was successful
        if self.workflow.build_process_failed:
            self.log.info("Not importing failed build")
            return

        if is_scratch_build(self.workflow):
            self.log.info('scratch build, skipping plugin')
            return

        if not self.imagestream_name:
            self.log.info('no imagestream provided, skipping plugin')
            return

        self.floating_images = get_floating_images(self.workflow)
        if not self.floating_images:
            self.log.info('No floating tags to import, skipping import_image')
            return

        self.resolve_docker_image_repo()

        self.osbs = get_openshift_session(self.workflow.conf,
                                          self.workflow.user_params.get('namespace'))
        self.get_or_create_imagestream()

        self.osbs.import_image_tags(self.imagestream_name, self.get_trackable_tags(),
                                    self.docker_image_repo, insecure=self.insecure_registry)

    @retry_on_conflict
    def get_or_create_imagestream(self):
        try:
            self.imagestream = self.osbs.get_image_stream(self.imagestream_name)
        except OsbsResponseException:
            self.log.info('Creating ImageStream %s for %s', self.imagestream_name,
                          self.docker_image_repo)

            self.imagestream = self.osbs.create_image_stream(self.imagestream_name)

    def get_trackable_tags(self):
        tags = []
        for floating_image in self.floating_images:
            tag = floating_image.tag
            tags.append(tag)

        return tags

    def resolve_docker_image_repo(self):
        # The plugin parameter docker_image_repo is actually a combination
        # of source_registry_uri and name label. Thus, the fallback case must
        # be handled in a non-generic way.
        source_registry = self.workflow.conf.source_registry
        registry = source_registry['uri'].docker_uri
        image = self.floating_images[0]
        image.registry = registry

        organization = self.workflow.conf.registries_organization
        if organization:
            image.enclose(organization)

        self.docker_image_repo = image.to_str(registry=True, tag=False)
