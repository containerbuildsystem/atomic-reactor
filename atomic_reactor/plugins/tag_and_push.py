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
from typing import Dict, List, Union

from atomic_reactor.constants import (
    DOCKER_PUSH_BACKOFF_FACTOR,
    DOCKER_PUSH_MAX_RETRIES,
    IMAGE_TYPE_DOCKER_ARCHIVE,
    IMAGE_TYPE_OCI,
    PLUGIN_FLATPAK_CREATE_OCI,
    PLUGIN_SOURCE_CONTAINER_KEY,
)
from atomic_reactor.config import get_koji_session
from atomic_reactor.plugin import Plugin
from atomic_reactor.plugins.fetch_sources import PLUGIN_FETCH_SOURCES_KEY
from atomic_reactor.metadata import annotation_map
from atomic_reactor.util import (Dockercfg, get_all_manifests,
                                 get_manifest_digests,
                                 get_platforms,
                                 is_flatpak_build,
                                 map_to_user_params)
from atomic_reactor.utils import retries
from osbs.utils import ImageName
import osbs.utils
from osbs.constants import RAND_DIGITS


__all__ = ('TagAndPushPlugin', )


@annotation_map('repositories')
class TagAndPushPlugin(Plugin):
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
        self.koji_target = koji_target

    def push_with_skopeo(self, image: Dict[str, str], registry_image: ImageName, insecure: bool,
                         docker_push_secret: str) -> None:
        cmd = ['skopeo', 'copy']
        if docker_push_secret is not None:
            dockercfg = Dockercfg(docker_push_secret)
            cmd.append('--authfile=' + dockercfg.json_secret_path)

        if insecure:
            cmd.append('--dest-tls-verify=false')

        if image['type'] == IMAGE_TYPE_OCI:
            # ref_name is added by 'flatpak_create_oci'
            # we have to be careful when changing the source container image type
            # since assumption here is that source container image will always be 'docker-archive'
            source_img = 'oci:{path}:{ref_name}'.format(**image)
            cmd.append('--format=v2s2')
        elif image['type'] == IMAGE_TYPE_DOCKER_ARCHIVE:
            source_img = 'docker-archive://{path}'.format(**image)
        else:
            raise RuntimeError("Attempt to push unsupported image type %s with skopeo" %
                               image['type'])

        dest_img = 'docker://' + registry_image.to_str()

        cmd += [source_img, dest_img]

        try:
            retries.run_cmd(cmd)
        except subprocess.CalledProcessError as e:
            self.log.error("push failed with output:\n%s", e.output)
            raise

    def source_get_unique_image(self) -> ImageName:
        source_result = self.workflow.data.plugins_results[PLUGIN_FETCH_SOURCES_KEY]
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

    def get_repositories(self) -> Dict[str, List[str]]:
        # usually repositories formed from NVR labels
        # these should be used for pulling and layering
        primary_repositories = []

        for image in self.workflow.data.tag_conf.primary_images:
            primary_repositories.append(image.to_str())

        # unique unpredictable repositories
        unique_repositories = []

        for image in self.workflow.data.tag_conf.unique_images:
            unique_repositories.append(image.to_str())

        # floating repositories
        # these should be used for pulling and layering
        floating_repositories = []

        for image in self.workflow.data.tag_conf.floating_images:
            floating_repositories.append(image.to_str())

        return {
            "primary": primary_repositories,
            "unique": unique_repositories,
            "floating": floating_repositories,
        }

    def run(self) -> Dict[str, Union[List, Dict[str, List[str]]]]:
        is_source_build = PLUGIN_FETCH_SOURCES_KEY in self.workflow.data.plugins_results

        if not is_source_build and not is_flatpak_build(self.workflow):
            self.log.info('not a flatpak or source build, skipping plugin')
            return {'pushed_images': [],
                    'repositories': self.get_repositories()}

        pushed_images = []
        wf_data = self.workflow.data

        tag_conf = wf_data.tag_conf

        images = []
        if is_source_build:
            source_image = self.source_get_unique_image()
            plugin_results = wf_data.plugins_results[PLUGIN_SOURCE_CONTAINER_KEY]
            image = plugin_results['image_metadata']
            tag_conf.add_unique_image(source_image)
            images.append((image, source_image))
        else:
            for image_platform in get_platforms(self.workflow.data):
                plugin_results = wf_data.plugins_results[PLUGIN_FLATPAK_CREATE_OCI]
                image = plugin_results[image_platform]
                registry_image = tag_conf.get_unique_images_with_platform(image_platform)[0]
                images.append((image, registry_image))

        insecure = self.registry.get('insecure', False)

        docker_push_secret = self.registry.get('secret', None)
        self.log.info("Registry %s secret %s", self.registry['uri'], docker_push_secret)

        for image, registry_image in images:
            max_retries = DOCKER_PUSH_MAX_RETRIES

            for retry in range(max_retries + 1):
                self.push_with_skopeo(image, registry_image, insecure, docker_push_secret)

                if is_source_build:
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

        return {'pushed_images': pushed_images,
                'repositories': self.get_repositories()}
