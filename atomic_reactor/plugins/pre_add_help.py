"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Convert a help markdown file a man page and store it to /help.1 in the image
so that 'atomic help' could display it.
This is accomplished by appending an ADD command to it.

Example configuration:
{
    'name': 'add_help',
    'args': {'help_file': 'help.md'}
}
"""

import errno
from datetime import datetime as dt
from pathlib import Path
from subprocess import check_output, CalledProcessError, STDOUT
from typing import List

from atomic_reactor import start_time as atomic_reactor_start_time
from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.metadata import annotation_map
from osbs.utils import Labels

DEFAULT_HELP_FILENAME = "help.md"


@annotation_map('help_file')
class AddHelpPlugin(PreBuildPlugin):
    key = "add_help"
    man_filename = "help.1"

    NO_HELP_FILE_FOUND = 1
    HELP_GENERATED = 2

    def __init__(self, workflow, help_file=DEFAULT_HELP_FILENAME):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :param help_file: filename of the markdown help file
        """
        # call parent constructor
        super(AddHelpPlugin, self).__init__(workflow)
        self.help_file = help_file

    def run(self):
        """
        run the plugin

        The plugin returns None if exception occurred,
        self.NO_HELP_FILE_FOUND if no help found
        or self.HELP_GENERATED if help man page was generated
        """
        if not (self.workflow.build_dir.any_platform.path / self.help_file).exists():
            self.log.info("File %s not present in the build directory", self.help_file)
            return {
                'help_file': None,
                'status': self.NO_HELP_FILE_FOUND
            }

        self.workflow.build_dir.for_all_platforms_copy(self.render_help_file)
        self.workflow.build_dir.for_each_platform(self.add_help_file_to_df)

        return {
            'help_file': self.help_file,
            'status': self.HELP_GENERATED
        }

    def render_help_file(self, build_dir: BuildDir) -> List[Path]:
        """Update the help.md file in the build directory and use it to generate a man file."""
        dockerfile = build_dir.dockerfile_with_parent_env(
            # platform should not matter, we only care about the component and maintainer labels
            self.workflow.imageutil.base_image_inspect()
        )
        labels = Labels(dockerfile.labels)
        try:
            _, name = labels.get_name_and_value(Labels.LABEL_TYPE_NAME)
        except KeyError:
            name = ''
        maintainer = dockerfile.labels.get('maintainer', '')

        help_path = build_dir.path / self.help_file

        with open(help_path, 'r+') as help_file:
            lines = help_file.readlines()

            if not lines[0].startswith("% "):
                lines.insert(0, "%% %s (1) Container Image Pages\n" % name)
                lines.insert(1, "%% %s\n" % maintainer)
                lines.insert(2, "%% %s\n" % dt.fromtimestamp(atomic_reactor_start_time)
                             .strftime(format="%B %-d, %Y"))

                help_file.seek(0)
                help_file.truncate()
                help_file.writelines(lines)

                self.log.info("added metadata to %s for generating nicer manpages", help_path)

        man_path = build_dir.path / self.man_filename

        go_md2man_cmd = ['go-md2man', f'-in={help_path}', f'-out={man_path}']

        try:
            check_output(go_md2man_cmd, stderr=STDOUT)
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise RuntimeError(
                    "Help file is available, but go-md2man is not present in a buildroot"
                ) from e

            raise
        except CalledProcessError as e:
            raise RuntimeError(
                "Error running %s: %s, exit code: %s, output: '%s'" % (
                    e.cmd, e, e.returncode, e.output
                )
            ) from e

        if not man_path.exists():
            raise RuntimeError("go-md2man run complete, but man file is not found")

        # We modified one file and created the other, let's copy both to all per-platform dirs
        return [help_path, man_path]

    def add_help_file_to_df(self, build_dir: BuildDir) -> None:
        """Include the generated man file in the Dockerfile."""
        dockerfile = build_dir.dockerfile
        lines = dockerfile.lines

        content = 'ADD {0} /{0}'.format(self.man_filename)
        # put it before last instruction
        lines.insert(-1, content + '\n')

        dockerfile.lines = lines

        self.log.info("added %s", build_dir.path / self.man_filename)
