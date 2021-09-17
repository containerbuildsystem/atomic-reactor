"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import argparse
import logging
import pkg_resources
import locale

from atomic_reactor import set_logging
from osbs import set_logging as set_logging_osbs
from atomic_reactor.constants import DESCRIPTION, PROG
from atomic_reactor.util import (setup_introspection_signal_handler,
                                 exception_message)


logger = logging.getLogger('atomic_reactor')


def construct_kwargs(**kwargs):
    ret = {}
    ret['source'] = {'provider_params': {}}

    # extend this when adding more args that should be passed to build_* functions
    recognized_kwargs = ['image', 'target_registries', 'target_registries_insecure',
                         'dont_pull_base_image']

    def is_recognized_kwarg(x):
        return x in recognized_kwargs or x.startswith('source__')

    for k, v in kwargs.items():
        if is_recognized_kwarg(k):
            if k.startswith('source__provider_params__'):
                ret['source']['provider_params'][k.split('__')[-1]] = v
            elif k.startswith('source__'):
                ret['source'][k.split('__')[-1]] = v
            else:
                ret[k] = v

    return ret


class CLI(object):
    def __init__(self, formatter_class=argparse.HelpFormatter, prog=PROG):
        self.parser = argparse.ArgumentParser(
            prog=prog,
            description=DESCRIPTION,
            formatter_class=formatter_class,
        )
        self.bi_parser = None
        self.ib_parser = None
        self.source_types_parsers = None

        locale.setlocale(locale.LC_ALL, '')

    def set_arguments(self):
        try:
            version = pkg_resources.get_distribution("atomic_reactor").version
        except pkg_resources.DistributionNotFound:
            version = "GIT"

        exclusive_group = self.parser.add_mutually_exclusive_group()
        exclusive_group.add_argument("-q", "--quiet", action="store_true")
        exclusive_group.add_argument("-v", "--verbose", action="store_true")
        exclusive_group.add_argument("-V", "--version", action="version", version=version)

        # subparsers = self.parser.add_subparsers(help='commands')

    def run(self):
        self.set_arguments()
        args = self.parser.parse_args()
        logging.captureWarnings(True)

        if args.verbose:
            set_logging(level=logging.DEBUG)
            set_logging_osbs(level=logging.DEBUG)
        elif args.quiet:
            set_logging(level=logging.WARNING)
            set_logging_osbs(level=logging.WARNING)
        else:
            set_logging(level=logging.INFO)
            set_logging_osbs(level=logging.INFO)
        try:
            args.func(args)
        except AttributeError:
            if hasattr(args, 'func'):
                raise
            else:
                self.parser.print_help()
        except KeyboardInterrupt:
            pass
        except Exception as ex:
            if args.verbose:
                raise
            else:
                logger.error("exception caught: %s", exception_message(ex))


def run():
    cli = CLI()
    setup_introspection_signal_handler()
    cli.run()


if __name__ == '__main__':
    run()
