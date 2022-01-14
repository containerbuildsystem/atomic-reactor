"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import functools
import json
import os
from copy import deepcopy

from osbs.utils import Labels

from atomic_reactor.constants import (IMAGE_BUILD_INFO_DIR, INSPECT_ROOTFS,
                                      INSPECT_ROOTFS_LAYERS,
                                      PLUGIN_ADD_IMAGE_CONTENT_MANIFEST,
                                      PLUGIN_FETCH_MAVEN_KEY,
                                      PLUGIN_RESOLVE_REMOTE_SOURCE)
from atomic_reactor.config import get_cachito_session
from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import read_yaml, read_content_sets, map_to_user_params
from atomic_reactor.utils.pnc import PNCUtil


class AddImageContentManifestPlugin(PreBuildPlugin):
    """
    Add the ICM JSON file to the IMAGE_BUILD_INFO_DIR/content_manifests
    directory, for the current platform. Filename will be '{IMAGE_NVR}.json'

    ICM examples:

    WITHOUT content_sets specified:

    {
      "metadata": {
        "icm_version": 1,
        "icm_spec": "https://link.to.icm.specification",
        "image_layer_index": 3
      },
      "content_sets" : [],
      "image_contents": [
        {
          "purl": "pkg:golang/github.com%2Frelease-engineering%2Fretrodep%2Fv2@v2.0.2",
          "dependencies": [{"purl": "pkg:golang/github.com%2Fop%2Fgo-logging@v0.0.0"}],
          "sources": [{"purl": "pkg:golang/github.com%2FMasterminds%2Fsemver@v1.4.2"}]
        }
      ]
    }

    WITH content_sets specified:

    {
      "metadata": {
        "icm_version": 1,
        "icm_spec": "https://link.to.icm.specification",
        "image_layer_index": 2
      },
      "content_sets": [
          "rhel-8-for-x86_64-baseos-rpms",
          "rhel-8-for-x86_64-appstream-rpms"
      ],
      "image_contents": [
        {
          "purl": "pkg:golang/github.com%2Frelease-engineering%2Fretrodep%2Fv2@v2.0.2",
          "dependencies": [{"purl": "pkg:golang/github.com%2Fop%2Fgo-logging@v0.0.0"}],
          "sources": [{"purl": "pkg:golang/github.com%2FMasterminds%2Fsemver@v1.4.2"}]
        }
      ]
    }
    """
    key = PLUGIN_ADD_IMAGE_CONTENT_MANIFEST
    is_allowed_to_fail = False
    minimal_icm = {
        'metadata': {
            'icm_version': 1,
            'icm_spec': ('https://raw.githubusercontent.com/containerbuildsystem/atomic-reactor/'
                         'master/atomic_reactor/schemas/content_manifest.json'),
            'image_layer_index': 1
        },
        'content_sets': [],
        'image_contents': [],
    }

    args_from_user_params = map_to_user_params("remote_sources")

    def __init__(self, workflow, destdir=IMAGE_BUILD_INFO_DIR):
        """
        :param workflow: DockerBuildWorkflow instance
        :param destdir: image path to carry content_manifests data dir
        """
        super(AddImageContentManifestPlugin, self).__init__(workflow)
        self.content_manifests_dir = os.path.join(destdir, 'content_manifests')

        remote_source_results = workflow.prebuild_results.get(PLUGIN_RESOLVE_REMOTE_SOURCE) or []
        self.remote_source_ids = [remote_source['id'] for remote_source in remote_source_results]

        fetch_maven_results = workflow.prebuild_results.get(PLUGIN_FETCH_MAVEN_KEY) or {}
        self.pnc_artifact_ids = fetch_maven_results.get('pnc_artifact_ids') or []

    @functools.cached_property
    def icm_file_name(self):
        """Determine the name for the ICM file (name-version-release.json)."""
        # parse Dockerfile for any platform, the N-V-R labels should be equal for all platforms
        dockerfile = self.workflow.build_dir.any_platform.dockerfile_with_parent_env(
            self.workflow.imageutil.base_image_inspect()
        )
        labels = Labels(dockerfile.labels)
        _, name = labels.get_name_and_value(Labels.LABEL_TYPE_COMPONENT)
        _, version = labels.get_name_and_value(Labels.LABEL_TYPE_VERSION)
        _, release = labels.get_name_and_value(Labels.LABEL_TYPE_RELEASE)
        return f"{name}-{version}-{release}.json"

    @property
    def layer_index(self) -> int:
        # inspect any platform, we expect the number of layers to be equal for all platforms
        inspect = self.workflow.imageutil.base_image_inspect()
        if not inspect:
            # Base images ('FROM koji/image-build') and 'FROM scratch' images do not have any
            #   base image. When building with `podman build --squash`, such images get squashed
            #   to only 1 layer => the layer index in this case is 0 (the first and only layer).

            # OSBS2 TBD: this is only true for build tasks that behave like `podman build --squash`
            return 0

        return len(inspect[INSPECT_ROOTFS][INSPECT_ROOTFS_LAYERS])

    @functools.cached_property
    def _icm_base(self) -> dict:
        """Create the platform-independent skeleton of the ICM document.

        :return: dict, the ICM as a Python dict
        """
        icm = deepcopy(self.minimal_icm)

        if self.remote_source_ids:
            icm = self.cachito_session.get_image_content_manifest(self.remote_source_ids)

        if self.pnc_artifact_ids:
            purl_specs = self.pnc_util.get_artifact_purl_specs(self.pnc_artifact_ids)
            for purl_spec in purl_specs:
                icm['image_contents'].append({'purl': purl_spec})

        icm['metadata']['image_layer_index'] = self.layer_index
        return icm

    def make_icm(self, platform: str) -> dict:
        """Create the complete ICM document for the specified platform."""
        icm = deepcopy(self._icm_base)

        content_sets = read_content_sets(self.workflow) or {}
        icm['content_sets'] = content_sets.get(platform, [])

        self.log.debug('Output ICM content_sets: %s', icm['content_sets'])
        self.log.debug('Output ICM metadata: %s', icm['metadata'])

        # Validate; `json.dumps()` converts `icm` to str. Confusingly, `read_yaml`
        #     *will* validate JSON
        read_yaml(json.dumps(icm), 'schemas/content_manifest.json')
        return icm

    def _write_json_file(self, icm: dict, build_dir: BuildDir) -> None:
        out_file_path = build_dir.path / self.icm_file_name
        if out_file_path.exists():
            raise RuntimeError(f'File {out_file_path} already exists in repo')

        with open(out_file_path, 'w') as outfile:
            json.dump(icm, outfile, indent=4)

        self.log.debug('ICM JSON saved to: %s', out_file_path)

    def _add_to_dockerfile(self, build_dir: BuildDir) -> None:
        """
        Put an ADD instruction into the Dockerfile (to include the ICM file
        into the container image to be built)
        """
        dest_file_path = os.path.join(self.content_manifests_dir, self.icm_file_name)
        content = 'ADD {0} {1}'.format(self.icm_file_name, dest_file_path)
        lines = build_dir.dockerfile.lines

        # Put it before last instruction
        lines.insert(-1, content + '\n')
        build_dir.dockerfile.lines = lines

    def inject_icm(self, build_dir: BuildDir) -> None:
        """Inject the ICM document to a build directory."""
        self.log.debug(
            "Injecting ICM to the build directory for the %s platform", build_dir.platform
        )
        icm = self.make_icm(build_dir.platform)
        self._write_json_file(icm, build_dir)
        self._add_to_dockerfile(build_dir)
        self.log.info('added "%s" to "%s"', self.icm_file_name, self.content_manifests_dir)

    def run(self):
        """Run the plugin."""
        self.workflow.build_dir.for_each_platform(self.inject_icm)

    @property
    def cachito_session(self):
        if not self.workflow.conf.cachito:
            raise RuntimeError('No Cachito configuration defined')
        return get_cachito_session(self.workflow.conf)

    @property
    def pnc_util(self):
        pnc_map = self.workflow.conf.pnc
        if not pnc_map:
            raise RuntimeError('No PNC configuration found in reactor config map')
        return PNCUtil(pnc_map)
