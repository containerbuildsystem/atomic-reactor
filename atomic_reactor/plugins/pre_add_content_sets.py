"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import json
import os

from atomic_reactor.constants import (PLUGIN_ADD_CONTENT_SETS, REPO_CONTENT_SETS_CONFIG,
                                      IMAGE_BUILD_INFO_DIR, INSPECT_ROOTFS, INSPECT_ROOTFS_LAYERS)
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import (df_parser, read_yaml_from_file_path, base_image_is_scratch)


class AddContentSetsPlugin(PreBuildPlugin):
    """
    Pre plugin will add metadata_{current_layer_index}.json in the IMAGE_BUILD_INFO_DIR,
    which will contain content sets for the current platform

    examples:
    in case content_sets were specified
    {
        "content_sets" : [
            "rhel-8-for-x86_64-baseos-rpms",
            "rhel-8-for-x86_64-appstream-rpms"],
    }

    in case no content_sets were specified
    {
        "content_sets" : []
    }

    """
    key = PLUGIN_ADD_CONTENT_SETS
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, destdir=IMAGE_BUILD_INFO_DIR):
        """
        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param destdir: directory in the image to put metadata_{layer_index}.json
        """
        super(AddContentSetsPlugin, self).__init__(tasker, workflow)

        self.cs_dir = destdir

    def get_output_json(self):
        current_platform = self.workflow.user_params['platform']
        workdir = self.workflow.builder.df_dir
        file_path = os.path.join(workdir, REPO_CONTENT_SETS_CONFIG)
        content_sets = {}

        if os.path.exists(file_path):
            content_sets = read_yaml_from_file_path(file_path, 'schemas/content_sets.json') or {}

        output_json = {'content_sets': []}
        if current_platform in content_sets:
            output_json['content_sets'] = content_sets[current_platform]

        self.log.debug('output json: %s', output_json)
        return output_json

    def write_json_file(self, file_name, data):
        file_path = os.path.join(self.workflow.builder.df_dir, file_name)

        if os.path.exists(file_path):
            raise RuntimeError('file {} already exists in repo'.format(file_path))

        with open(file_path, 'w') as outfile:
            json.dump(data, outfile)

        self.log.debug('output json saved to: %s', file_path)

    def get_layer_index(self, dfp):
        # default layer index is 1, because FROM scratch will have
        # 2 layers and we are using index
        layer_index = 1

        if not base_image_is_scratch(dfp.baseimage):
            inspect = self.workflow.builder.base_image_inspect

            layer_index = len(inspect[INSPECT_ROOTFS][INSPECT_ROOTFS_LAYERS])

        return layer_index

    def run(self):
        """
        run the plugin
        """
        output_json = self.get_output_json()

        dfp = df_parser(self.workflow.builder.df_path, workflow=self.workflow)

        layer_index = self.get_layer_index(dfp)

        output_file_name = 'metadata_{}.json'.format(layer_index)
        output_path = os.path.join(self.cs_dir, output_file_name)

        self.write_json_file(output_file_name, output_json)

        lines = dfp.lines

        content = 'ADD {0} {1}'.format(output_file_name, output_path)

        # put it before last instruction
        lines.insert(-1, content + '\n')
        dfp.lines = lines

        self.log.info("added %s", output_path)
