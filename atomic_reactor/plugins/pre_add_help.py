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
import os
from subprocess import check_output, CalledProcessError, STDOUT
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import df_parser

DEFAULT_HELP_FILENAME = "help.md"


class AddHelpPlugin(PreBuildPlugin):
    key = "add_help"
    man_filename = "help.1"

    NO_HELP_FILE_FOUND = 1
    HELP_GENERATED = 2

    def __init__(self, tasker, workflow, help_file=DEFAULT_HELP_FILENAME):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param help_file: filename of the markdown help file
        """
        # call parent constructor
        super(AddHelpPlugin, self).__init__(tasker, workflow)
        self.help_file = help_file

    def run(self):
        """
        run the plugin

        The plugin returns None if exception occurred,
        self.NO_HELP_FILE_FOUND if no help found
        or self.HELP_GENERATED if help man page was generated
        """

        result = {
            'help_file': self.help_file,
            'status': None
        }

        help_path = os.path.join(self.workflow.builder.df_dir, self.help_file)

        if not os.path.exists(help_path):
            self.log.info("File %s not found", help_path)
            result['status'] = self.NO_HELP_FILE_FOUND
            return result

        man_path = os.path.join(self.workflow.builder.df_dir, self.man_filename)

        go_md2man_cmd = ['go-md2man', '-in={}'.format(help_path), '-out={}'.format(man_path)]

        try:
            check_output(go_md2man_cmd, stderr=STDOUT)
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise RuntimeError(
                    "Help file is available, but go-md2man is not present in a buildroot")

            raise
        except CalledProcessError as e:
            raise RuntimeError("Error running %s: %r, exit code: %s, output: '%s'" % (
                e.cmd, e, e.returncode, e.output))

        if not os.path.exists(man_path):
            raise RuntimeError("go-md2man run complete, but man file is not found")

        # Include the help file in the docker file
        dockerfile = df_parser(self.workflow.builder.df_path, workflow=self.workflow)
        lines = dockerfile.lines

        content = 'ADD {0} /{0}'.format(self.man_filename)
        # put it before last instruction
        lines.insert(-1, content + '\n')

        dockerfile.lines = lines

        self.log.info("added %s", man_path)

        result['status'] = self.HELP_GENERATED
        return result
