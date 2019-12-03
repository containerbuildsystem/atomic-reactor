"""
Copyright (c) 2015, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

from osbs.exceptions import OsbsResponseException
from osbs.utils import retry_on_conflict

from atomic_reactor.plugin import PostBuildPlugin, ExitPlugin
from atomic_reactor.util import get_floating_images, ImageName
from atomic_reactor.plugins.pre_reactor_config import (get_openshift_session, get_source_registry,
                                                       get_registries_organization)


# Note: We use multiple inheritance here only to make it explicit that
# this plugin needs to act as both an exit plugin (since arrangement
# version 6) and as a post-build plugin (arrangement version < 6). In
# fact, ExitPlugin is a subclass of PostBuildPlugin.
class ImportImagePlugin(ExitPlugin, PostBuildPlugin):
    """
    Import image tags from external docker registry into Origin,
    creating an ImageStream if one does not already exist.
    """

    key = 'import_image'
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, imagestream, docker_image_repo=None,
                 url=None, build_json_dir=None, verify_ssl=True, use_auth=True,
                 insecure_registry=None):
        """
        constructor

        :param tasker: ContainerTasker instance
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
        self.imagestream_name = imagestream

        self.openshift_fallback = {
            'url': url,
            'insecure': not verify_ssl,
            'auth': {'enable': use_auth},
            'build_json_dir': build_json_dir
        }

        self.insecure_registry = get_source_registry(
            self.workflow, {'insecure': insecure_registry})['insecure']

        self.osbs = None
        self.imagestream = None
        self.floating_images = None
        self.docker_image_repo = None
        self.docker_image_repo_fallback = docker_image_repo

    def run(self):
        # Only run if the build was successful
        if self.workflow.build_process_failed:
            self.log.info("Not importing failed build")
            return

        self.floating_images = get_floating_images(self.workflow)
        if not self.floating_images:
            self.log.info('No floating tags to import, skipping import_image')
            return

        self.resolve_docker_image_repo()

        self.osbs = get_openshift_session(self.workflow, self.openshift_fallback)
        self.get_or_create_imagestream()

        try:
            self.osbs.import_image_tags(self.imagestream_name, self.get_trackable_tags(),
                                        self.docker_image_repo, insecure=self.insecure_registry)
        except AttributeError:
            self.log.info('Falling back to calling import_image instead of import_image_tags')
            self.process_tags()
            self.osbs.import_image(self.imagestream_name, tags=self.get_trackable_tags())

    @retry_on_conflict
    def get_or_create_imagestream(self):
        try:
            self.imagestream = self.osbs.get_image_stream(self.imagestream_name)
        except OsbsResponseException:
            kwargs = {}
            if self.insecure_registry is not None:
                kwargs['insecure_registry'] = self.insecure_registry

            self.log.info('Creating ImageStream %s for %s', self.imagestream_name,
                          self.docker_image_repo)

            self.imagestream = self.osbs.create_image_stream(self.imagestream_name,
                                                             self.docker_image_repo,
                                                             **kwargs)

    def process_tags(self):
        self.log.info('Importing new tags for %s', self.imagestream_name)
        failures = False

        for tag in self.get_trackable_tags():
            try:
                self.osbs.ensure_image_stream_tag(self.imagestream.json(), tag)
                self.log.info('Imported ImageStreamTag: (%s)', tag)
            except OsbsResponseException:
                failures = True
                self.log.info('Could not import ImageStreamTag: (%s)', tag)

        if failures:
            raise RuntimeError('Failed to import ImageStreamTag(s). Check logs')

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
        try:
            source_registry = get_source_registry(self.workflow)
        except KeyError:
            image = ImageName.parse(self.docker_image_repo_fallback)
        else:
            registry = source_registry['uri'].docker_uri
            image = self.floating_images[0]
            image.registry = registry

        organization = get_registries_organization(self.workflow)
        if organization:
            image.enclose(organization)

        self.docker_image_repo = image.to_str(registry=True, tag=False)
