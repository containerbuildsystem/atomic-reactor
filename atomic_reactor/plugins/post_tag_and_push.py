"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import

from copy import deepcopy
import subprocess
import time
import platform
import random

from atomic_reactor.constants import (IMAGE_TYPE_DOCKER_ARCHIVE, IMAGE_TYPE_OCI, IMAGE_TYPE_OCI_TAR,
                                      MEDIA_TYPE_DOCKER_V2_SCHEMA2, DOCKER_PUSH_MAX_RETRIES,
                                      DOCKER_PUSH_BACKOFF_FACTOR)
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.plugins.exit_remove_built_image import defer_removal
from atomic_reactor.plugins.pre_reactor_config import (get_registries, get_group_manifests,
                                                       get_koji_session,
                                                       get_registries_organization,
                                                       get_image_size_limit)
from atomic_reactor.plugins.pre_fetch_sources import PLUGIN_FETCH_SOURCES_KEY
from atomic_reactor.util import (get_manifest_digests, get_config_from_registry, Dockercfg,
                                 get_all_manifests)
from osbs.utils import ImageName
import osbs.utils
from osbs.constants import RAND_DIGITS


__all__ = ('TagAndPushPlugin', )


class ExceedsImageSizeError(RuntimeError):
    """Error of exceeding image size"""


class TagAndPushPlugin(PostBuildPlugin):
    """
    Use tags from workflow.tag_conf and push the images to workflow.push_conf
    """

    key = "tag_and_push"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, registries=None, koji_target=None):
        """
        constructor

        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param registries: dict, keys are docker registries, values are dicts containing
                           per-registry parameters.
                           Params:
                            * "insecure" optional boolean - controls whether pushes are allowed over
                              plain HTTP.
                            * "secret" optional string - path to the secret, which stores
                              email, login and password for remote registry
        :param koji_target: str, used only for sourcecontainers
        """
        # call parent constructor
        super(TagAndPushPlugin, self).__init__(tasker, workflow)

        self.registries = get_registries(self.workflow, deepcopy(registries or {}))
        self.group = get_group_manifests(self.workflow, False)
        self.koji_target = koji_target

    def need_skopeo_push(self):
        if len(self.workflow.exported_image_sequence) > 0:
            last_image = self.workflow.exported_image_sequence[-1]
            if last_image['type'] == IMAGE_TYPE_OCI or last_image['type'] == IMAGE_TYPE_OCI_TAR:
                return True

        return False

    def push_with_skopeo(self, registry_image, insecure, docker_push_secret,
                         source_oci_image_path=None):
        cmd = ['skopeo', 'copy']
        if docker_push_secret is not None:
            dockercfg = Dockercfg(docker_push_secret)
            cmd.append('--authfile=' + dockercfg.json_secret_path)

        if insecure:
            cmd.append('--dest-tls-verify=false')

        if not source_oci_image_path:
            # If the last image has type OCI_TAR, then hunt back and find the
            # the untarred version, since skopeo only supports OCI's as an
            # untarred directory
            image = [x for x in self.workflow.exported_image_sequence if
                     x['type'] != IMAGE_TYPE_OCI_TAR][-1]

            if image['type'] == IMAGE_TYPE_OCI:
                source_img = 'oci:{path}:{ref_name}'.format(**image)
            elif image['type'] == IMAGE_TYPE_DOCKER_ARCHIVE:
                source_img = 'docker-archive://{path}'.format(**image)
            else:
                raise RuntimeError("Attempt to push unsupported image type %s with skopeo" %
                                   image['type'])
        else:
            source_img = 'oci:{}'.format(source_oci_image_path)

        dest_img = 'docker://' + registry_image.to_str()

        cmd += [source_img, dest_img]

        self.log.info("Calling: %s", ' '.join(cmd))
        try:
            subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            self.log.error("push failed with output:\n%s", e.output)
            raise

    def source_get_unique_image(self):
        source_result = self.workflow.prebuild_results[PLUGIN_FETCH_SOURCES_KEY]
        koji_build_id = source_result['sources_for_koji_build_id']
        kojisession = get_koji_session(self.workflow)

        timestamp = osbs.utils.utcnow().strftime('%Y%m%d%H%M%S')
        random.seed()
        current_platform = platform.processor() or 'x86_64'

        tag_segments = [
            self.koji_target or 'none',
            str(random.randrange(10**(RAND_DIGITS - 1), 10**RAND_DIGITS)),
            timestamp,
            current_platform
        ]

        tag = '-'.join(tag_segments)

        get_build_meta = kojisession.getBuild(koji_build_id)
        pull_specs = get_build_meta['extra']['image']['index']['pull']
        source_image_spec = ImageName.parse(pull_specs[0])
        source_image_spec.tag = tag
        organization = get_registries_organization(self.workflow)
        if organization:
            source_image_spec.enclose(organization)
        source_image_spec.registry = None
        return source_image_spec

    def run(self):
        pushed_images = []

        source_oci_image_path = self.workflow.build_result.oci_image_path
        if source_oci_image_path:
            source_unique_image = self.source_get_unique_image()

        if not self.workflow.tag_conf.unique_images:
            if source_oci_image_path:
                self.workflow.tag_conf.add_unique_image(source_unique_image)
            else:
                self.workflow.tag_conf.add_unique_image(self.workflow.image)

        config_manifest_digest = None
        config_manifest_type = None
        config_registry_image = None
        image_size_limit = get_image_size_limit(self.workflow)

        for registry, registry_conf in self.registries.items():
            insecure = registry_conf.get('insecure', False)
            push_conf_registry = \
                self.workflow.push_conf.add_docker_registry(registry, insecure=insecure)

            docker_push_secret = registry_conf.get('secret', None)
            self.log.info("Registry %s secret %s", registry, docker_push_secret)

            for image in self.workflow.tag_conf.images:
                if image.registry:
                    raise RuntimeError("Image name must not contain registry: %r" % image.registry)

                if not source_oci_image_path:
                    image_size = sum(item['size'] for item in self.workflow.layer_sizes)
                    config_image_size = image_size_limit['binary_image']
                    # Only handle the case when size is set > 0 in config
                    if config_image_size and image_size > config_image_size:
                        raise ExceedsImageSizeError(
                            'The size {} of image {} exceeds the limitation {} '
                            'configured in reactor config.'
                            .format(image_size, image, image_size_limit)
                        )

                registry_image = image.copy()
                registry_image.registry = registry
                max_retries = DOCKER_PUSH_MAX_RETRIES

                expect_v2s2 = False
                for registry in self.registries:
                    media_types = self.registries[registry].get('expected_media_types', [])
                    if MEDIA_TYPE_DOCKER_V2_SCHEMA2 in media_types:
                        expect_v2s2 = True

                if not (self.group or expect_v2s2):
                    max_retries = 0

                for retry in range(max_retries + 1):
                    if self.need_skopeo_push() or source_oci_image_path:
                        self.push_with_skopeo(registry_image, insecure, docker_push_secret,
                                              source_oci_image_path)
                    else:
                        self.tasker.tag_and_push_image(self.workflow.builder.image_id,
                                                       registry_image, insecure=insecure,
                                                       force=True, dockercfg=docker_push_secret)

                    if source_oci_image_path:
                        manifests_dict = get_all_manifests(registry_image, registry, insecure,
                                                           docker_push_secret, versions=('v2',))
                        try:
                            koji_source_manifest_response = manifests_dict['v2']
                        except KeyError:
                            raise RuntimeError('Unable to fetch v2 schema 2 digest for {}'.
                                               format(registry_image.to_str()))

                        self.workflow.koji_source_manifest = koji_source_manifest_response.json()

                    digests = get_manifest_digests(registry_image, registry,
                                                   insecure, docker_push_secret)

                    if (not (digests.v2 or digests.oci) and (retry < max_retries)):
                        sleep_time = DOCKER_PUSH_BACKOFF_FACTOR * (2 ** retry)
                        self.log.info("Retrying push because V2 schema 2 or "
                                      "OCI manifest not found in %is", sleep_time)

                        time.sleep(sleep_time)
                    else:
                        if not self.need_skopeo_push():
                            defer_removal(self.workflow, registry_image)
                        break

                pushed_images.append(registry_image)

                tag = registry_image.to_str(registry=False)
                push_conf_registry.digests[tag] = digests

                if not config_manifest_digest and (digests.v2 or digests.oci):
                    if digests.v2:
                        config_manifest_digest = digests.v2
                        config_manifest_type = 'v2'
                    else:
                        config_manifest_digest = digests.oci
                        config_manifest_type = 'oci'
                    config_registry_image = registry_image

            if config_manifest_digest:
                push_conf_registry.config = get_config_from_registry(
                    config_registry_image, registry, config_manifest_digest, insecure,
                    docker_push_secret, config_manifest_type)
            else:
                self.log.info("V2 schema 2 or OCI manifest is not available to get config from")

        self.log.info("All images were tagged and pushed")
        return pushed_images
