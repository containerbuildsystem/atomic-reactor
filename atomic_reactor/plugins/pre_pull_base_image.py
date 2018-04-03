"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pull base image to build our layer on.
"""

from __future__ import unicode_literals

import docker

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import get_build_json, get_manifest_list, ImageName, DefaultKeyDict
from atomic_reactor.constants import (PLUGIN_BUILD_ORCHESTRATE_KEY,
                                      PLUGIN_CHECK_AND_SET_PLATFORMS_KEY)
from atomic_reactor.core import RetryGeneratorException
from atomic_reactor.plugins.pre_reactor_config import get_source_registry, get_platform_descriptors
from osbs.utils import RegistryURI


class PullBaseImagePlugin(PreBuildPlugin):
    key = "pull_base_image"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, parent_registry=None, parent_registry_insecure=False,
                 check_platforms=False):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param parent_registry: registry to enforce pulling from
        :param parent_registry_insecure: allow connecting to the registry over plain http
        """
        # call parent constructor
        super(PullBaseImagePlugin, self).__init__(tasker, workflow)

        self.check_platforms = check_platforms
        source_registry = get_source_registry(self.workflow, {
            'uri': RegistryURI(parent_registry) if parent_registry else None,
            'insecure': parent_registry_insecure})

        if source_registry.get('uri'):
            self.parent_registry = source_registry['uri'].docker_uri
            self.parent_registry_insecure = source_registry['insecure']
        else:
            self.parent_registry = None
            self.parent_registry_insecure = False

    def resolve_base_image(self, build_json):
        spec = build_json.get("spec")
        try:
            image_id = spec['triggeredBy'][0]['imageChangeBuild']['imageID']
        except (TypeError, KeyError, IndexError):
            base_image = self.workflow.builder.base_image
        else:
            base_image = ImageName.parse(image_id)  # any exceptions will propagate

        return base_image

    def run(self):
        """
        pull base image
        """
        build_json = get_build_json()

        base_image = self.resolve_base_image(build_json)

        base_image_with_registry = base_image.copy()

        if self.parent_registry:
            self.log.info("pulling base image '%s' from registry '%s'",
                          base_image, self.parent_registry)
            # registry in dockerfile doesn't match provided source registry
            if base_image.registry and base_image.registry != self.parent_registry:
                self.log.error("registry in dockerfile doesn't match provided source registry, "
                               "dockerfile = '%s', provided = '%s'",
                               base_image.registry, self.parent_registry)
                raise RuntimeError(
                    "Registry specified in dockerfile doesn't match provided one. "
                    "Dockerfile: '%s', Provided: '%s'"
                    % (base_image.registry, self.parent_registry))

            base_image_with_registry.registry = self.parent_registry
        else:
            self.log.info("pulling base image '%s'", base_image)

        if self.check_platforms:
            self.validate_platforms_in_base_image(base_image_with_registry)

        try:
            self.tasker.pull_image(base_image_with_registry,
                                   insecure=self.parent_registry_insecure)

        except RetryGeneratorException as original_exc:
            if base_image_with_registry.namespace == 'library':
                raise

            self.log.info("'%s' not found", base_image_with_registry.to_str())
            base_image_with_registry.namespace = 'library'
            self.log.info("trying '%s'", base_image_with_registry.to_str())

            try:
                self.tasker.pull_image(base_image_with_registry,
                                       insecure=self.parent_registry_insecure)

            except RetryGeneratorException:
                raise original_exc

        pulled_base = base_image_with_registry.to_str()
        self.workflow.pulled_base_images.add(pulled_base)

        # Attempt to tag it using a unique ID. We might have to retry
        # if another build with the same parent image is finishing up
        # and removing images it pulled.

        # Use the OpenShift build name as the unique ID
        unique_id = build_json['metadata']['name']
        original_base_image = base_image.copy()
        base_image = ImageName(repo=unique_id)

        for _ in range(20):
            try:
                self.log.info("tagging pulled image")
                response = self.tasker.tag_image(base_image_with_registry,
                                                 base_image)
                self.workflow.pulled_base_images.add(response)
                break
            except docker.errors.NotFound:
                # If we get here, some other build raced us to remove
                # the parent image, and that build won.
                # Retry the pull immediately.
                self.log.info("re-pulling removed image")
                self.tasker.pull_image(base_image_with_registry,
                                       insecure=self.parent_registry_insecure)
        else:
            # Failed to tag it
            self.log.error("giving up trying to pull image")
            raise RuntimeError("too many attempts to pull and tag image")

        self.workflow.builder.set_base_image(base_image.to_str())
        self.workflow.builder.original_base_image = ImageName.parse(original_base_image.to_str())
        self.log.debug("image '%s' is available", pulled_base)

    def validate_platforms_in_base_image(self, base_image):
        expected_platforms = self.get_expected_platforms()
        if not expected_platforms:
            self.log.info('Skipping validation of available platforms '
                          'because expected platforms are unknown')
            return
        if len(expected_platforms) == 1:
            self.log.info('Skipping validation of available platforms for base image '
                          'because this is a single platform build')
            return

        if not base_image.registry:
            self.log.info('Cannot validate available platforms for base image '
                          'because base image registry is not defined')
            return

        try:
            platform_descriptors = get_platform_descriptors(self.workflow)
        except KeyError:
            self.log.info('Cannot validate available platforms for base image '
                          'because platform descriptors are not defined')
            return

        manifest_list = get_manifest_list(base_image, base_image.registry,
                                          insecure=self.parent_registry_insecure)
        if not manifest_list:
            raise RuntimeError('Unable to fetch manifest list for base image')

        all_manifests = manifest_list.json()['manifests']
        manifest_list_arches = set(
            manifest['platform']['architecture'] for manifest in all_manifests)

        platform_to_arch = DefaultKeyDict(
            (descriptor['platform'], descriptor['architecture'])
            for descriptor in platform_descriptors)

        expected_arches = set(
            platform_to_arch[platform] for platform in expected_platforms)

        self.log.info('Manifest list arches: %s, expected arches: %s',
                      manifest_list_arches, expected_arches)
        assert manifest_list_arches >= expected_arches, \
            'Missing arches in manifest list for base image'

        self.log.info('Base image is a manifest list for all required platforms')

    def get_expected_platforms(self):
        platforms = self.workflow.prebuild_results.get(PLUGIN_CHECK_AND_SET_PLATFORMS_KEY)
        if platforms:
            return platforms

        for plugin in self.workflow.buildstep_plugins_conf or []:
            if plugin['name'] == PLUGIN_BUILD_ORCHESTRATE_KEY:
                return plugin['args']['platforms']
