#!/usr/bin/python
"""
Heavily inspired by:

https://github.com/pwman3/pwman3/blob/d718a01fa8038893e42416b59cdfcda3935fe878/build_manpage.py

"""

from collections import OrderedDict
import datetime
import argparse

from atomic_reactor.cli.main import CLI
from atomic_reactor.constants import MANPAGE_AUTHORS, DESCRIPTION, PROG, MANPAGE_SECTION


class ManPageGenerator(object):
    def __init__(self):
        self.cli = CLI()
        self.cli.set_arguments()
        self.output = "atomic-reactor.%d" % MANPAGE_SECTION
        self.today = datetime.date.today()

    def run(self):
        mpf = ManPageFormatter(PROG,
                               desc=DESCRIPTION,
                               ext_sections={
                                   "authors": MANPAGE_AUTHORS,
                               })

        build_subparsers = OrderedDict(sorted(
            [(p, OrderedDict()) for p in self.cli.source_types_parsers.values()],
            key=lambda p: p[0].prog
        ))
        m = mpf.format_man_page(main_parser=self.cli.parser, subparsers=OrderedDict([
            (self.cli.build_parser, build_subparsers),
            (self.cli.bi_parser, OrderedDict()),
            (self.cli.ib_parser, OrderedDict()),
        ]))
        with open(self.output, 'w') as f:
            f.write(m)


class ManPageFormatter(argparse.HelpFormatter):
    def __init__(self,
                 prog,
                 max_help_position=120,
                 width=160,
                 section=MANPAGE_SECTION,
                 desc=None,
                 long_desc=None,
                 ext_sections=None,
                 ):

        super(ManPageFormatter, self).__init__(prog, width=width,
                                               max_help_position=max_help_position)

        self._prog = prog
        self._section = section
        self._today = datetime.date.today().strftime('%Y\\-%m\\-%d')
        self._desc = desc
        self._long_desc = long_desc
        self._ext_sections = ext_sections

    def _markup(self, txt):
        return txt.replace('-', '\\-')

    def _underline(self, string):
        return "\\fI\\s-1" + string + "\\s0\\fR"

    def _bold(self, string):
        if not string.strip().startswith('\\fB'):
            string = '\\fB' + string
        if not string.strip().endswith('\\fR'):
            string = string + '\\fR'
        return string

    def create_main_synopsis(self, parser):
        """ create synopsis from main parser """
        self.add_usage(parser.usage, parser._actions,
                       parser._mutually_exclusive_groups, prefix='')
        usage = self._format_usage(None, parser._actions,
                                   parser._mutually_exclusive_groups, '')

        usage = usage.replace('%s ' % self._prog, '')
        usage = '.SH SYNOPSIS\n \\fB%s\\fR %s\n' % (self._markup(self._prog),
                                                    usage)
        return usage

    def create_subcommand_synopsis(self, parser):
        """ show usage with description for commands """
        self.add_usage(parser.usage, parser._get_positional_actions(),
                       None, prefix='')
        usage = self._format_usage(parser.usage, parser._get_positional_actions(),
                                   None, '')
        return self._bold(usage)

    def create_title(self, prog):
        return '.TH {0} {1} {2}\n'.format(prog, self._section,
                                          self._today)

    def create_name(self, parser):
        return '.SH NAME\n%s \\- %s\n' % (parser.prog,
                                          parser.description)

    def create_description(self):
        if self._long_desc:
            long_desc = self._long_desc.replace('\n', '\n.br\n')
            return '.SH DESCRIPTION\n%s\n' % self._markup(long_desc)
        else:
            return ''

    def create_footer(self, sections):
        if not hasattr(sections, '__iter__'):
            return ''

        footer = []
        for section, value in sections.items():
            part = ".SH {}\n {}".format(section.upper(), value)
            footer.append(part)

        return '\n'.join(footer)

    def format_man_page(self, main_parser, subparsers):
        page = []
        page.append(self.create_title(self._prog))
        page.append(self.create_main_synopsis(main_parser))
        page.append(self.create_description())
        page.append(self.create_options(main_parser))
        page.append(self.create_commands(subparsers))
        page.append(self.create_footer(self._ext_sections))

        return ''.join(page)

    def create_options(self, main_parser):
        formatter = main_parser._get_formatter()

        for action_group in main_parser._action_groups:
            # options and arguments are just enough
            formatter.start_section(None)
            formatter.add_text(None)
            formatter.add_arguments(action_group._group_actions)
            formatter.end_section()

        formatted_helps = formatter.format_help()

        return '.SH OPTIONS\n' + formatted_helps

    def create_commands(self, subparsers):
        formatted_helps = ""
        for parser, nested_subparsers in subparsers.items():
            formatter = parser._get_formatter()
            # mention optional arguments only, not positional: those should be in usage
            action_group = parser._action_groups[1]
            formatter.start_section(self.create_subcommand_synopsis(parser))
            formatter.add_text(parser.description)
            formatter.add_arguments(action_group._group_actions)
            formatter.end_section()

            formatter.add_text(parser.epilog)

            generated_help = formatter.format_help()
            formatted_help = ""
            count = 0
            # whitespace magic
            for line in generated_help.split("\n"):
                count += 1
                line = line.rstrip().rstrip(":")
                if count == 1:
                    formatted_help += "\n\n" + line + "\n.PP"
                    continue
                if line:
                    formatted_help += line + "\n"
            formatted_helps += formatted_help
            if nested_subparsers:
                formatted_helps += self.create_commands(nested_subparsers)
        return '\n\n.SH COMMANDS\n' + formatted_helps

    def _format_action_invocation(self, action):
        if not action.option_strings:
            metavar, = self._metavar_formatter(action, action.dest)(1)
            return metavar

        else:
            parts = []

            # if the Optional doesn't take a value, format is:
            #    -s, --long
            if action.nargs == 0:
                parts.extend([self._bold(action_str) for action_str in action.option_strings])

            # if the Optional takes a value, format is:
            #    -s ARGS, --long ARGS
            else:
                default = self._underline(action.dest.upper())
                args_string = self._format_args(action, default)
                for option_string in action.option_strings:
                    parts.append('%s %s' % (self._bold(option_string), args_string))

            return ', '.join(parts)


mg = ManPageGenerator()
mg.run()
