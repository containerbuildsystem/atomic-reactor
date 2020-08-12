"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import, unicode_literals

import json
import os

from copy import deepcopy

from osbs.utils import Labels

from atomic_reactor.constants import (IMAGE_BUILD_INFO_DIR, INSPECT_ROOTFS,
                                      INSPECT_ROOTFS_LAYERS,
                                      PLUGIN_ADD_IMAGE_CONTENT_MANIFEST,
                                      REPO_CONTENT_SETS_CONFIG)
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_reactor_config import get_cachito
from atomic_reactor.util import (base_image_is_scratch, df_parser, read_yaml,
                                 read_yaml_from_file_path, get_retrying_requests_session,
                                 )


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

    def __init__(self, tasker, workflow, remote_source_icm_url=None, destdir=IMAGE_BUILD_INFO_DIR):
        """
        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param icm_url: str, URL of the ICM from the Cachito request.
        :param destdir: image path to carry content_manifests data dir
        """
        super(AddImageContentManifestPlugin, self).__init__(tasker, workflow)
        self.content_manifests_dir = os.path.join(destdir, 'content_manifests')
        self.icm_url = remote_source_icm_url
        self.dfp = df_parser(self.workflow.builder.df_path, workflow=self.workflow)
        labels = Labels(self.dfp.labels)
        _, image_name = labels.get_name_and_value(Labels.LABEL_TYPE_COMPONENT)
        _, image_version = labels.get_name_and_value(Labels.LABEL_TYPE_VERSION)
        _, image_release = labels.get_name_and_value(Labels.LABEL_TYPE_RELEASE)
        self.icm_file_name = '{}-{}-{}.json'.format(image_name, image_version, image_release)
        self.content_sets = []
        self._cachito_verify = None
        self._layer_index = None
        self._icm = None

    @property
    def layer_index(self):
        if self._layer_index is None:
            # Default layer index is 1, because base and 'FROM scratch' images
            #     *always* have 2 layers
            self._layer_index = 1
        if not base_image_is_scratch(self.dfp.baseimage):
            inspect = self.workflow.builder.base_image_inspect
            self._layer_index = len(inspect[INSPECT_ROOTFS][INSPECT_ROOTFS_LAYERS])
        return self._layer_index

    @property
    def cachito_verify(self):
        if self._cachito_verify is None:
            try:
                cachito_conf = get_cachito(self.workflow)
            except KeyError:
                cachito_conf = {}
            # Get the value of Cachito's 'insecure' key from the active reactor config map,
            #    *flip it*, and let the result tell us whether to verify or not
            self._cachito_verify = not cachito_conf.get('insecure', False)
        return self._cachito_verify

    @property
    def icm(self):
        """
        Get and validate the ICM from the Cachito API `content-manifest` endpoint.

        :return: dict, the ICM as a Python dict
        """
        if self.icm_url is None and self._icm is None:
            self._icm = deepcopy(self.minimal_icm)
        if self._icm is None:
            session = get_retrying_requests_session()
            session.verify = self.cachito_verify
            self.log.debug('Making request to "%s"', self.icm_url)
            response = session.get(self.icm_url)
            response.raise_for_status()
            self._icm = response.json()  # Returns dict

            # Validate; `json.dumps()` converts `icm` to str. Confusingly, `read_yaml`
            #     *will* validate JSON
            read_yaml(json.dumps(self._icm), 'schemas/content_manifest.json')
        return self._icm

    def _populate_content_sets(self):
        """
        Get the list of the current platform's content_sets from
        'content_sets.yml' in dist-git, and set `self.content_sets` to same.
        """
        current_platform = self.workflow.user_params['platform']
        workdir = self.workflow.builder.df_dir
        fpath = os.path.join(workdir, REPO_CONTENT_SETS_CONFIG)
        if os.path.exists(fpath):
            content_sets_yml = read_yaml_from_file_path(fpath, 'schemas/content_sets.json') or {}
            if current_platform in content_sets_yml:
                self.content_sets = content_sets_yml[current_platform]
        self.log.debug('Output content_sets: %s', self.content_sets)

    def _update_icm_data(self):
        # Inject the content_sets data into the ICM JSON object
        self.icm['content_sets'] = self.content_sets

        # Inject the current image layer index number into the ICM JSON object metadata
        self.icm['metadata']['image_layer_index'] = self.layer_index

        # Convert dict -> str
        icm_json = json.dumps(self.icm, indent=4)

        # Validate the updated ICM with the ICM JSON Schema
        read_yaml(icm_json, 'schemas/content_manifest.json')

        self.log.debug('Output ICM content_sets: %s', self.icm['content_sets'])
        self.log.debug('Output ICM metadata: %s', self.icm['metadata'])

    def _write_json_file(self):
        out_file_path = os.path.join(self.workflow.builder.df_dir,
                                     self.icm_file_name)
        if os.path.exists(out_file_path):
            raise RuntimeError('File {} already exists in repo'.format(out_file_path))
        with open(out_file_path, 'w') as outfile:
            json.dump(self.icm, outfile, indent=4)
        self.log.debug('ICM JSON saved to: %s', out_file_path)

    def _add_to_dockerfile(self):
        """
        Put an ADD instruction into the Dockerfile (to include the ICM file
        into the container image to be built)
        """
        dest_file_path = os.path.join(self.content_manifests_dir, self.icm_file_name)
        content = 'ADD {0} {1}'.format(self.icm_file_name, dest_file_path)
        lines = self.dfp.lines

        # Put it before last instruction
        lines.insert(-1, content + '\n')
        self.dfp.lines = lines

    def run(self):
        """
        run the plugin
        """
        self._populate_content_sets()
        self._update_icm_data()
        self._write_json_file()
        self._add_to_dockerfile()
        self.log.info('added "%s" to "%s"', self.icm_file_name, self.content_manifests_dir)
