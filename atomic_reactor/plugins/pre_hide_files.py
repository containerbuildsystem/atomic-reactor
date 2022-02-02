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
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.constants import INSPECT_CONFIG, SCRATCH_FROM


class DockerfileStage(NamedTuple):
    """Delimits a single stage in a Dockerfile.

    from_structure: information about the FROM instruction for this stage
    end_structure: information about the last instruction in this stage
    user_line: the last USER line in this stage, if any
    """

    from_structure: Dict[str, Any]
    end_structure: Dict[str, Any]
    user_line: Optional[str]


class HideFilesPlugin(PreBuildPlugin):
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

        hide_in_build_dir = functools.partial(self._add_hide_lines, start_lines, end_lines)
        self.workflow.build_dir.for_each_platform(hide_in_build_dir)

    def _add_hide_lines(
        self, start_lines: List[str], end_lines: List[str], build_dir: BuildDir
    ) -> None:
        """Add the hide instructions to every stage of the Dockerfile in this build dir."""
        dockerfile = build_dir.dockerfile
        stages = self._find_stages(dockerfile)

        # For each stage, wrap it with the extra lines we want.
        # Work backwards to preserve line numbers.
        for stage in reversed(stages):
            self._update_dockerfile(dockerfile, stage, start_lines, end_lines)

    def _find_stages(self, dockerfile: DockerfileParser) -> List[DockerfileStage]:
        """Find limits of each Dockerfile stage"""
        stages = []
        end = None
        last_user_found = None

        for part in reversed(dockerfile.structure):
            if end is None:
                end = part

            if part['instruction'] == 'USER' and not last_user_found:
                # we will reuse the line verbatim
                last_user_found = part['content']

            if part['instruction'] == 'FROM':
                stage = DockerfileStage(
                    from_structure=part, end_structure=end, user_line=last_user_found
                )
                stages.append(stage)
                end = None
                last_user_found = None

        # we found the stages in reverse order, return them in the correct order
        stages.reverse()
        return stages

    def _update_dockerfile(
        self,
        dockerfile: DockerfileParser,
        stage: DockerfileStage,
        start_lines: List[str],
        end_lines: List[str]
    ) -> None:
        """Add the specified lines at the start and end of the specified stage in the Dockerfile."""
        from_structure, end_structure, stage_user = stage

        self.log.debug("updating stage starting line %d, ending at %d",
                       from_structure['startline'], end_structure['endline'])
        add_start_lines = []
        add_end_lines = []

        parent_image_id = from_structure['value'].split(' ', 1)[0]

        if parent_image_id == SCRATCH_FROM:
            return

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
        dockerfile.add_lines_at(from_structure, *add_start_lines, after=True)
