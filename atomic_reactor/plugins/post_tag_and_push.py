"""
Copyright (c) 2015-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import subprocess
import time
import platform
import random

from atomic_reactor.constants import (IMAGE_TYPE_DOCKER_ARCHIVE, IMAGE_TYPE_OCI, IMAGE_TYPE_OCI_TAR,
                                      DOCKER_PUSH_MAX_RETRIES, DOCKER_PUSH_BACKOFF_FACTOR)
from atomic_reactor.config import get_koji_session
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.plugins.pre_fetch_sources import PLUGIN_FETCH_SOURCES_KEY
from atomic_reactor.util import (Dockercfg, get_all_manifests, map_to_user_params,
                                 get_manifest_digests)
from atomic_reactor.utils import retries
from osbs.utils import ImageName
import osbs.utils
from osbs.constants import RAND_DIGITS


__all__ = ('TagAndPushPlugin', )


class ExceedsImageSizeError(RuntimeError):
    """Error of exceeding image size"""


class TagAndPushPlugin(PostBuildPlugin):
    """
    Use tags from workflow.data.tag_conf and push the images to workflow.conf.registry
    """

    key = "tag_and_push"
    is_allowed_to_fail = False

    args_from_user_params = map_to_user_params("koji_target")

    def __init__(self, workflow, koji_target=None):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :param koji_target: str, used only for sourcecontainers
        """
        # call parent constructor
        super(TagAndPushPlugin, self).__init__(workflow)

        self.registry = self.workflow.conf.registry
        self.group = self.workflow.conf.group_manifests
        self.koji_target = koji_target

    def need_skopeo_push(self):
        if len(self.workflow.data.exported_image_sequence) > 0:
            last_image = self.workflow.data.exported_image_sequence[-1]
            if last_image['type'] == IMAGE_TYPE_OCI or last_image['type'] == IMAGE_TYPE_OCI_TAR:
                return True

        return False

    def push_with_skopeo(self, registry_image, insecure, docker_push_secret,
                         source_docker_archive=None):
        cmd = ['skopeo', 'copy']
        if docker_push_secret is not None:
            dockercfg = Dockercfg(docker_push_secret)
            cmd.append('--authfile=' + dockercfg.json_secret_path)

        if insecure:
            cmd.append('--dest-tls-verify=false')

        if not source_docker_archive:
            # If the last image has type OCI_TAR, then hunt back and find the
            # the untarred version, since skopeo only supports OCI's as an
            # untarred directory
            image = [x for x in self.workflow.data.exported_image_sequence if
                     x['type'] != IMAGE_TYPE_OCI_TAR][-1]

            if image['type'] == IMAGE_TYPE_OCI:
                source_img = 'oci:{path}:{ref_name}'.format(**image)
            elif image['type'] == IMAGE_TYPE_DOCKER_ARCHIVE:
                source_img = 'docker-archive://{path}'.format(**image)
            else:
                raise RuntimeError("Attempt to push unsupported image type %s with skopeo" %
                                   image['type'])
        else:
            source_img = 'docker-archive:{}'.format(source_docker_archive)

        dest_img = 'docker://' + registry_image.to_str()

        cmd += [source_img, dest_img]

        try:
            retries.run_cmd(cmd)
        except subprocess.CalledProcessError as e:
            self.log.error("push failed with output:\n%s", e.output)
            raise

    def source_get_unique_image(self):
        source_result = self.workflow.data.prebuild_results[PLUGIN_FETCH_SOURCES_KEY]
        koji_build_id = source_result['sources_for_koji_build_id']
        kojisession = get_koji_session(self.workflow.conf)

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
        organization = self.workflow.conf.registries_organization
        if organization:
            source_image_spec.enclose(organization)
        source_image_spec.registry = self.workflow.conf.registry['uri']
        return source_image_spec

    def run(self):
        pushed_images = []
        wf_data = self.workflow.data

        source_docker_archive = wf_data.build_result.source_docker_archive
        if source_docker_archive:
            source_unique_image = self.source_get_unique_image()

        tag_conf = wf_data.tag_conf
        if not tag_conf.unique_images:
            if source_docker_archive:
                tag_conf.add_unique_image(source_unique_image)
            else:
                tag_conf.add_unique_image(self.workflow.image)

        image_size_limit = self.workflow.conf.image_size_limit

        insecure = self.registry.get('insecure', False)

        docker_push_secret = self.registry.get('secret', None)
        self.log.info("Registry %s secret %s", self.registry['uri'], docker_push_secret)

        for image in wf_data.tag_conf.images:
            if not source_docker_archive:
                # OSBS2 TBD
                # layer_sizes were removed from workflow data
                # these should be fetched from imageutil method
                image_size = sum(item['size'] for item in self.workflow.data.layer_sizes)
                config_image_size = image_size_limit['binary_image']
                # Only handle the case when size is set > 0 in config
                if config_image_size and image_size > config_image_size:
                    raise ExceedsImageSizeError(
                        'The size {} of image {} exceeds the limitation {} '
                        'configured in reactor config.'
                        .format(image_size, image, image_size_limit)
                    )

            registry_image = image.copy()
            max_retries = DOCKER_PUSH_MAX_RETRIES

            for retry in range(max_retries + 1):
                if self.need_skopeo_push() or source_docker_archive:
                    self.push_with_skopeo(registry_image, insecure, docker_push_secret,
                                          source_docker_archive)
                else:
                    # OSBS2 TBD either use store manifest from ManifestUtil
                    # or tag_imag from utils.image
                    # we won't need pushing
                    # self.tasker.tag_and_push_image(self.workflow.data.image_id,
                    #                                registry_image, insecure=insecure,
                    #                                force=True, dockercfg=docker_push_secret)
                    pass

                if source_docker_archive:
                    manifests_dict = get_all_manifests(registry_image, self.registry['uri'],
                                                       insecure,
                                                       docker_push_secret, versions=('v2',))
                    try:
                        koji_source_manifest_response = manifests_dict['v2']
                    except KeyError as exc:
                        raise RuntimeError(
                            f'Unable to fetch v2 schema 2 digest for {registry_image.to_str()}'
                        ) from exc

                    wf_data.koji_source_manifest = koji_source_manifest_response.json()

                digests = get_manifest_digests(registry_image, self.registry['uri'],
                                               insecure, docker_push_secret)

                if not (digests.v2 or digests.oci) and (retry < max_retries):
                    sleep_time = DOCKER_PUSH_BACKOFF_FACTOR * (2 ** retry)
                    self.log.info("Retrying push because V2 schema 2 or "
                                  "OCI manifest not found in %is", sleep_time)

                    time.sleep(sleep_time)
                else:
                    break

            pushed_images.append(registry_image)

        self.log.info("All images were tagged and pushed")
        return pushed_images
