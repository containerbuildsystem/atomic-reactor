"""
Copyright (c) 2017, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import print_function, unicode_literals, absolute_import

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.constants import (
    INSPECT_CONFIG, PLUGIN_KOJI_PARENT_KEY, BASE_IMAGE_KOJI_BUILD, PARENT_IMAGES_KOJI_BUILDS,
    KOJI_BTYPE_IMAGE
)
from atomic_reactor.plugins.pre_reactor_config import (
    get_deep_manifest_list_inspection, get_koji_session, get_source_registry,
    get_skip_koji_check_for_base_image, get_fail_on_digest_mismatch
)
from atomic_reactor.util import (
    base_image_is_custom, get_manifest_list, get_manifest_media_type, is_scratch_build
)
from copy import copy
from osbs.utils import Labels

import json
import koji
import time


DEFAULT_POLL_TIMEOUT = 60 * 10  # 10 minutes
DEFAULT_POLL_INTERVAL = 10  # 10 seconds


class KojiParentBuildMissing(ValueError):
    """Expected to find a build for the parent image in koji, did not find it within timeout."""

class KojiParentPlugin(PreBuildPlugin):
    """Wait for Koji build of parent images to be available

    Uses inspected parent image configs to determine the
    nvrs (Name-Version-Release) of the parent images. It uses
    this information to check if the corresponding Koji
    builds exist. This check is performed periodically until
    the Koji builds are all found, or timeout expires.

    This check is required due to a timing issue that may
    occur after the image is pushed to registry, but it
    has not been yet uploaded and tagged in Koji. This plugin
    ensures that the layered image is only built with parent
    images that are known in Koji.
    """

    key = PLUGIN_KOJI_PARENT_KEY
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, koji_hub=None, koji_ssl_certs_dir=None,
                 poll_interval=DEFAULT_POLL_INTERVAL, poll_timeout=DEFAULT_POLL_TIMEOUT):
        """
        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param koji_hub: str, koji hub (xmlrpc)
        :param koji_ssl_certs_dir: str, path to "cert", "ca", and "serverca"
                                   used when Koji's identity certificate is not trusted
        :param poll_interval: int, seconds between polling for Koji build
        :param poll_timeout: int, max amount of seconds to wait for Koji build
        """
        super(KojiParentPlugin, self).__init__(tasker, workflow)

        self.koji_fallback = {
            'hub_url': koji_hub,
            'auth': {'ssl_certs_dir': koji_ssl_certs_dir}
        }
        self.koji_session = get_koji_session(self.workflow, self.koji_fallback)

        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

        self._base_image_nvr = None
        self._base_image_build = None
        self._parent_builds = {}
        self._poll_start = None
        self._source_registry = get_source_registry(workflow, {})
        self._deep_manifest_list_inspection = get_deep_manifest_list_inspection(self.workflow,
                                                                                fallback=True)

    def run(self):
        if is_scratch_build(self.workflow):
            self.log.info('scratch build, skipping plugin')
            return

        if not (self.workflow.builder.base_from_scratch or self.workflow.builder.custom_base_image):
            self._base_image_nvr = self.detect_parent_image_nvr(
                self.workflow.builder.base_image,
                inspect_data=self.workflow.builder.base_image_inspect,
            )

        manifest_mismatches = []
        for img, local_tag in self.workflow.builder.parent_images.items():
            if base_image_is_custom(img.to_str()):
                continue

            nvr = self.detect_parent_image_nvr(local_tag) if local_tag else None
            self._parent_builds[img] = self.wait_for_parent_image_build(nvr) if nvr else None
            if nvr == self._base_image_nvr:
                self._base_image_build = self._parent_builds[img]

            if self._parent_builds[img]:
                # we need the possible floating tag
                check_img = copy(local_tag)
                check_img.tag = img.tag
                try:
                    self.check_manifest_digest(check_img, self._parent_builds[img])
                except ValueError as exc:
                    manifest_mismatches.append(exc)
            else:
                err_msg = ('Could not get koji build info for parent image {}. '
                           'Was this image built in OSBS?'.format(img.to_str()))
                if get_skip_koji_check_for_base_image(self.workflow, fallback=False):
                    self.log.warning(err_msg)
                else:
                    self.log.error(err_msg)
                    raise RuntimeError(err_msg)

        if manifest_mismatches:
            mismatch_msg = ('Error while comparing parent images manifest digests in koji with '
                            'related values from registries: %s')
            if get_fail_on_digest_mismatch(self.workflow, fallback=True):
                self.log.error(mismatch_msg, manifest_mismatches)
                raise RuntimeError(mismatch_msg % manifest_mismatches)

            self.log.warning(mismatch_msg, manifest_mismatches)
        return self.make_result()

    def check_manifest_digest(self, image, build_info):
        """Check if the manifest list digest is correct.

        Compares the manifest list digest with the value in koji metadata.
        Raises a ValueError if the manifest list does not refer to the koji build.

        :param image: ImageName, image to inspect
        :param build_info: dict, koji build metadata
        """
        image_str = image.to_str()
        v2_list_type = get_manifest_media_type('v2_list')
        v2_type = get_manifest_media_type('v2')
        image_digest_data = self.workflow.builder.parent_images_digests[image_str]
        if v2_list_type in image_digest_data:
            media_type = v2_list_type
        elif v2_type in image_digest_data:
            media_type = v2_type
        else:
            # This should not happen - raise just to be safe:
            raise RuntimeError('Unexpected parent image digest data for {}. '
                               'v2 or v2_list expected, got {}'.format(image, image_digest_data))

        digest = image_digest_data[media_type]

        try:
            koji_digest = build_info['extra']['image']['index']['digests'][media_type]
        except KeyError:
            err_msg = ("Koji build ({}) for parent image '{}' does not have manifest digest data "
                       "for the expected media type '{}'. This parent image MUST be rebuilt"
                       .format(build_info['id'], image_str, media_type))
            self.log.error(err_msg)
            raise ValueError(err_msg)

        expected_digest = koji_digest
        self.log.info('Verifying manifest digest (%s) for parent %s against its '
                      'koji reference (%s)', digest, image_str, expected_digest)
        if digest != expected_digest:
            rebuild_msg = 'This parent image MUST be rebuilt'
            mismatch_msg = ('Manifest digest (%s) for parent image %s does not match value in its '
                            'koji reference (%s). %s')
            if not self._deep_manifest_list_inspection:
                self.log.error(mismatch_msg, digest, image_str, expected_digest, rebuild_msg)
                raise ValueError(mismatch_msg % (digest, image_str, expected_digest, rebuild_msg))

            deep_inspection_msg = 'Checking manifest list contents...'
            self.log.warning(mismatch_msg, digest, image_str, expected_digest, deep_inspection_msg)
            if not self.manifest_list_entries_match(image, build_info['id']):
                err_msg = ('Manifest list for parent image %s differs from the manifest list for '
                           'its koji reference. %s')
                self.log.error(err_msg, image_str, rebuild_msg)
                raise ValueError(err_msg % (image_str, rebuild_msg))

    def manifest_list_entries_match(self, image, build_id):
        """Check whether manifest list entries are in koji.

        Compares the digest in each manifest list entry with the koji build
        archive for the entry's architecture. Returns True if they all match.

        :param image: ImageName, image to inspect
        :param build_id: int, koji build ID for the image

        :return: bool, True if the manifest list content refers to the koji build archives
        """
        if not image.registry:
            self.log.warning('Could not fetch manifest list for %s: missing registry ref', image)
            return False

        v2_type = get_manifest_media_type('v2')

        insecure = self._source_registry.get('insecure', False)
        dockercfg_path = self._source_registry.get('secret')
        manifest_list_response = get_manifest_list(image, image.registry, insecure=insecure,
                                                   dockercfg_path=dockercfg_path)
        if not manifest_list_response:
            self.log.warning('Could not fetch manifest list for %s', image)
            return False

        manifest_list_data = {}
        manifest_list = json.loads(manifest_list_response.content)
        for manifest in manifest_list['manifests']:
            if manifest['mediaType'] != v2_type:
                self.log.warning('Unexpected media type in manifest list: %s', manifest)
                return False

            arch = manifest['platform']['architecture']
            v2_digest = manifest['digest']
            manifest_list_data[arch] = v2_digest

        archives = self.koji_session.listArchives(build_id)
        koji_archives_data = {}
        for archive in (a for a in archives if a['btype'] == KOJI_BTYPE_IMAGE):
            arch = archive['extra']['docker']['config']['architecture']
            v2_digest = archive['extra']['docker']['digests'][v2_type]
            koji_archives_data[arch] = v2_digest

        if koji_archives_data == manifest_list_data:
            self.log.info('Deeper manifest list check verified v2 manifest references match')
            return True
        self.log.warning('Manifest list refs "%s" do not match koji archive refs "%s"',
                         manifest_list_data, koji_archives_data)
        return False

    def detect_parent_image_nvr(self, image_name, inspect_data=None):
        """
        Look for the NVR labels, if any, in the image.

        :return NVR string if labels found, otherwise None
        """

        if inspect_data is None:
            inspect_data = self.workflow.builder.parent_image_inspect(image_name)
        labels = Labels(inspect_data[INSPECT_CONFIG].get('Labels', {}))

        label_names = [Labels.LABEL_TYPE_COMPONENT, Labels.LABEL_TYPE_VERSION,
                       Labels.LABEL_TYPE_RELEASE]
        label_values = []

        for lbl_name in label_names:
            try:
                _, lbl_value = labels.get_name_and_value(lbl_name)
                label_values.append(lbl_value)
            except KeyError:
                self.log.info("Failed to find label '%s' in parent image '%s'.",
                              labels.get_name(lbl_name), image_name)

        if len(label_values) != len(label_names):  # don't have all the necessary labels
            self.log.info("Image '%s' NVR missing; not searching for Koji build.", image_name)
            return None

        return '-'.join(label_values)

    def wait_for_parent_image_build(self, nvr):
        """
        Given image NVR, wait for the build that produced it to show up in koji.
        If it doesn't within the timeout, raise an error.

        :return build info dict with 'nvr' and 'id' keys
        """

        self.log.info('Waiting for Koji build for parent image %s', nvr)
        poll_start = time.time()
        while time.time() - poll_start < self.poll_timeout:
            build = self.koji_session.getBuild(nvr)
            if build:
                self.log.info('Parent image Koji build found with id %s', build.get('id'))
                if build['state'] == koji.BUILD_STATES['COMPLETE']:
                    return build
                elif build['state'] != koji.BUILD_STATES['BUILDING']:
                    exc_msg = ('Parent image Koji build for {} with id {} state is not COMPLETE.')
                    raise KojiParentBuildMissing(exc_msg.format(nvr, build.get('id')))
            time.sleep(self.poll_interval)
        raise KojiParentBuildMissing('Parent image Koji build NOT found for {}!'.format(nvr))

    def make_result(self):
        """Construct the result dict to be preserved in the build metadata."""
        result = {}
        if self._base_image_build:
            result[BASE_IMAGE_KOJI_BUILD] = self._base_image_build
        if self._parent_builds:
            result[PARENT_IMAGES_KOJI_BUILDS] = self._parent_builds
        return result if result else None
