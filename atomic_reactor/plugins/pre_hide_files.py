"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import absolute_import

import os

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_reactor_config import get_hide_files
from atomic_reactor.util import df_parser, ImageName
from atomic_reactor.constants import INSPECT_CONFIG


class HideFilesPlugin(PreBuildPlugin):
    key = 'hide_files'
    is_allowed_to_fail = True

    def __init__(self, tasker, workflow):
        """
        Plugin initializer

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        """
        super(HideFilesPlugin, self).__init__(tasker, workflow)
        self.start_lines = []
        self.end_lines = []

    def run(self):
        """
        run the plugin
        """
        try:
            self.hide_files = get_hide_files(self.workflow)
        except KeyError:
            self.log.info("Skipping hide files: no files to hide")
            return

        self._populate_start_file_lines()
        self._populate_end_file_lines()

        for parent_image_id in self.workflow.builder.parent_images.values():
            self._update_dockerfile(parent_image_id)

    def _populate_start_file_lines(self):
        for file_to_hide in self.hide_files['files']:
            self.start_lines.append('RUN mv -f {} {} || :'.format(file_to_hide,
                                                                  self.hide_files['tmpdir']))

    def _populate_end_file_lines(self):
        for file_to_hide in self.hide_files['files']:
            file_base = os.path.basename(file_to_hide)
            tmp_dest = os.path.join(self.hide_files['tmpdir'], file_base)

            self.end_lines.append('RUN mv -fZ {} {} || :'.format(tmp_dest, file_to_hide))

    def _update_dockerfile(self, parent_image_id):
        dfp = df_parser(self.workflow.builder.df_path)
        add_start_lines = []
        add_end_lines = []

        inspect = self.workflow.builder.parent_image_inspect(parent_image_id)
        inherited_user = inspect.get(INSPECT_CONFIG).get('User', '')

        if inherited_user:
            add_start_lines.append('USER root')

        add_start_lines.extend(self.start_lines)

        if inherited_user:
            add_start_lines.append('USER {}'.format(inherited_user))

        parent_structure = self._find_parent_structure(dfp, parent_image_id)
        dfp.add_lines_at(parent_structure, *add_start_lines, after=True)

        final_user_line = "USER " + inherited_user if inherited_user else None

        last_user_found = None
        for insndesc in reversed(dfp.structure):
            if insndesc['instruction'] == 'USER' and not last_user_found:
                last_user_found = insndesc['content']  # we will reuse the line verbatim

            if insndesc['instruction'] == 'FROM':
                found_parent_image_id = ImageName.parse(insndesc['value'].split(' ')[0])
                if found_parent_image_id == parent_image_id:
                    break  # found last user for specific stage
                else:
                    last_user_found = None  # this wasn't our stage, resetting user

        if last_user_found:
            final_user_line = last_user_found

        if final_user_line:
            add_end_lines.append('USER root')

        add_end_lines.extend(self.end_lines)

        if final_user_line:
            add_end_lines.append(final_user_line)

        last_stage_structure = self._find_last_stage_structure(dfp, parent_structure)
        dfp.add_lines_at(last_stage_structure, *add_end_lines, after=True)

    def _find_parent_structure(self, dfp, parent_image_id):
        for structure in dfp.structure:
            if structure['instruction'] != 'FROM':
                continue
            found_parent_image_id = ImageName.parse(structure['value'].split(' ')[0])
            if found_parent_image_id == parent_image_id:
                return structure

        raise RuntimeError('Unable to find parent image instruction')

    def _find_last_stage_structure(self, dfp, parent_structure):
        partial_structure = dfp.structure[dfp.structure.index(parent_structure)+1:]
        last_structure = parent_structure
        for structure in partial_structure:
            if structure['instruction'] == 'FROM':
                break
            last_structure = structure
        return last_structure
