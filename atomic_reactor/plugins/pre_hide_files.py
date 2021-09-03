"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import df_parser
from atomic_reactor.constants import INSPECT_CONFIG, SCRATCH_FROM


class HideFilesPlugin(PreBuildPlugin):
    key = 'hide_files'
    is_allowed_to_fail = True

    def __init__(self, tasker, workflow):
        """
        Plugin initializer

        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        """
        super(HideFilesPlugin, self).__init__(tasker, workflow)
        self.start_lines = []
        self.end_lines = []
        self.dfp = None

    def run(self):
        """
        run the plugin
        """
        hide_files = self.workflow.conf.hide_files
        if not hide_files:
            self.log.info("Skipping hide files: no files to hide")
            return

        self._populate_start_file_lines(hide_files)
        self._populate_end_file_lines(hide_files)

        self.dfp = df_parser(self.workflow.df_path)
        stages = self._find_stages()

        # For each stage, wrap it with the extra lines we want.
        # Work backwards to preserve line numbers.
        for stage in reversed(stages):
            self._update_dockerfile(**stage)

    def _find_stages(self):
        """Find limits of each Dockerfile stage"""
        stages = []
        end = last_user_found = None
        for part in reversed(self.dfp.structure):
            if end is None:
                end = part

            if part['instruction'] == 'USER' and not last_user_found:
                # we will reuse the line verbatim
                last_user_found = part['content']

            if part['instruction'] == 'FROM':
                stages.insert(0, {'from_structure': part,
                                  'end_structure': end,
                                  'stage_user': last_user_found})
                end = last_user_found = None

        return stages

    def _populate_start_file_lines(self, hide_files):
        for file_to_hide in hide_files['files']:
            self.start_lines.append('RUN mv -f {} {} || :'.format(file_to_hide,
                                                                  hide_files['tmpdir']))

    def _populate_end_file_lines(self, hide_files):
        for file_to_hide in hide_files['files']:
            file_base = os.path.basename(file_to_hide)
            tmp_dest = os.path.join(hide_files['tmpdir'], file_base)

            self.end_lines.append('RUN mv -fZ {} {} || :'.format(tmp_dest, file_to_hide))

    def _update_dockerfile(self, from_structure, end_structure, stage_user):
        self.log.debug("updating stage starting line %d, ending at %d",
                       from_structure['startline'], end_structure['endline'])
        add_start_lines = []
        add_end_lines = []

        parent_image_id = from_structure['value'].split(' ', 1)[0]

        if parent_image_id == SCRATCH_FROM:
            return

        inspect = self.workflow.builder.parent_image_inspect(parent_image_id)
        inherited_user = inspect.get(INSPECT_CONFIG).get('User', '')

        if inherited_user:
            add_start_lines.append('USER root')

        add_start_lines.extend(self.start_lines)

        if inherited_user:
            add_start_lines.append('USER {}'.format(inherited_user))

        final_user_line = "USER " + inherited_user if inherited_user else None

        if stage_user:
            final_user_line = stage_user

        if final_user_line:
            add_end_lines.append('USER root')

        add_end_lines.extend(self.end_lines)

        if final_user_line:
            add_end_lines.append(final_user_line)

        self.log.debug("append after: %r", add_end_lines)
        self.log.debug("insert before: %r", add_start_lines)
        self.dfp.add_lines_at(end_structure, *add_end_lines, after=True)
        self.dfp.add_lines_at(from_structure, *add_start_lines, after=True)
