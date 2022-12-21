"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import functools
from pathlib import Path
from typing import NamedTuple, Dict, Any, Optional, List

from dockerfile_parse import DockerfileParser

from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugin import Plugin
from atomic_reactor.constants import INSPECT_CONFIG, SCRATCH_FROM
from atomic_reactor.util import base_image_is_custom


class DockerfileStage(NamedTuple):
    """Delimits a single stage in a Dockerfile.

    from_structure: information about the FROM instruction for this stage
    after_from_structure: information about the first instruction after FROM for this stage,
        used for baseimage scratch stage
    end_structure: information about the last instruction in this stage
    user_line: the last USER line in this stage, if any
    """

    from_structure: Dict[str, Any]
    after_from_structure: Optional[Dict[str, Any]]
    end_structure: Dict[str, Any]
    user_line: Optional[str]


class HideFilesPlugin(Plugin):
    key = 'hide_files'
    is_allowed_to_fail = True

    def run(self):
        """
        run the plugin
        """
        hide_files = self.workflow.conf.hide_files
        if not hide_files:
            self.log.info("Skipping hide files: no files to hide")
            return

        custom_image_index = self._get_custom_image_index()

        files_to_hide = list(map(Path, hide_files["files"]))
        tmpdir = Path(hide_files["tmpdir"])

        # At the start of the build, hide files by moving them to the configured tmpdir
        start_lines = [
            f"RUN mv -f {file_to_hide} {tmpdir} || :" for file_to_hide in files_to_hide
        ]
        # At the end of the build, move the hidden files back to their original locations
        end_lines = [
            f"RUN mv -fZ {tmpdir / file_to_hide.name} {file_to_hide} || :"
            for file_to_hide in files_to_hide
        ]

        hide_in_build_dir = functools.partial(self._add_hide_lines, custom_image_index,
                                              start_lines, end_lines)
        self.workflow.build_dir.for_each_platform(hide_in_build_dir)

    def _get_custom_image_index(self) -> List[int]:
        """get indexes for baseimage scratch stages"""
        custom_image_index = []
        df_images = self.workflow.data.dockerfile_images.original_parents

        for idx, img in enumerate(reversed(df_images)):
            if base_image_is_custom(str(img)):
                custom_image_index.append(idx)

        self.log.debug("custom reverse idx: '%s'", custom_image_index)
        return custom_image_index

    def _add_hide_lines(
        self, custom_image_index: List[int], start_lines: List[str], end_lines: List[str],
        build_dir: BuildDir
    ) -> None:
        """Add the hide instructions to every stage of the Dockerfile in this build dir."""
        dockerfile = build_dir.dockerfile
        stages = self._find_stages(dockerfile)

        # For each stage, wrap it with the extra lines we want.
        # Work backwards to preserve line numbers.
        for idx, stage in enumerate(reversed(stages)):
            self._update_dockerfile(dockerfile, stage, idx, start_lines, end_lines,
                                    custom_image_index)

    def _find_stages(self, dockerfile: DockerfileParser) -> List[DockerfileStage]:
        """Find limits of each Dockerfile stage"""
        stages = []
        end = None
        last_user_found = None
        after_from = None

        for part in reversed(dockerfile.structure):
            if end is None:
                end = part

            if part['instruction'] == 'USER' and not last_user_found:
                # we will reuse the line verbatim
                last_user_found = part['content']

            if part['instruction'] == 'FROM':
                stage = DockerfileStage(
                    from_structure=part, after_from_structure=after_from, end_structure=end,
                    user_line=last_user_found
                )
                stages.append(stage)
                after_from = None
                end = None
                last_user_found = None
            else:
                after_from = part

        # we found the stages in reverse order, return them in the correct order
        stages.reverse()
        return stages

    def _update_dockerfile(
        self,
        dockerfile: DockerfileParser,
        stage: DockerfileStage,
        idx: int,
        start_lines: List[str],
        end_lines: List[str],
        custom_image_index: List[int]
    ) -> None:
        """Add the specified lines at the start and end of the specified stage in the Dockerfile."""
        from_structure, after_from_structure, end_structure, stage_user = stage

        self.log.debug("updating stage starting line %d, ending at %d",
                       from_structure['startline'], end_structure['endline'])
        add_start_lines = []
        add_end_lines = []
        base_image_stage = False

        parent_image_id = from_structure['value'].split(' ', 1)[0]

        if parent_image_id == SCRATCH_FROM:
            if idx not in custom_image_index:
                return
            # is scratch stage but for baseimage
            else:
                base_image_stage = True

        inherited_user = ''
        if not base_image_stage:
            # inspect any platform, the parent user should be the same for all platforms
            inspect = self.workflow.imageutil.get_inspect_for_image(parent_image_id)
            inherited_user = inspect.get(INSPECT_CONFIG, {}).get('User', '')

        if inherited_user:
            add_start_lines.append('USER root')

        add_start_lines.extend(start_lines)

        if inherited_user:
            add_start_lines.append('USER {}'.format(inherited_user))

        final_user_line = "USER " + inherited_user if inherited_user else None

        if stage_user:
            final_user_line = stage_user

        if final_user_line:
            add_end_lines.append('USER root')

        add_end_lines.extend(end_lines)

        if final_user_line:
            add_end_lines.append(final_user_line)

        self.log.debug("append after: %r", add_end_lines)
        self.log.debug("insert before: %r", add_start_lines)
        dockerfile.add_lines_at(end_structure, *add_end_lines, after=True)
        if base_image_stage:
            dockerfile.add_lines_at(after_from_structure, *add_start_lines, after=True)
        else:
            dockerfile.add_lines_at(from_structure, *add_start_lines, after=True)
