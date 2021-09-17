"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import argparse
import logging
import sys
import pkg_resources
import locale

from atomic_reactor import set_logging
from osbs import set_logging as set_logging_osbs
from atomic_reactor.constants import DESCRIPTION, PROG
from atomic_reactor.buildimage import BuildImageBuilder
from atomic_reactor.inner import build_inside
from atomic_reactor.util import (setup_introspection_signal_handler,
                                 exception_message)


logger = logging.getLogger('atomic_reactor')


def cli_create_build_image(args):
    b = BuildImageBuilder(reactor_tarball_path=args.reactor_tarball_path,
                          reactor_local_path=args.reactor_local_path,
                          reactor_remote_path=args.reactor_remote_git,
                          use_official_reactor_git=args.reactor_latest)
    try:
        b.create_image(args.dockerfile_dir_path, args.image, use_cache=args.use_cache)
    except RuntimeError:
        logger.error("build failed")
        sys.exit(1)
    sys.exit(0)


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


def cli_inside_build(args):
    build_inside(input_method=args.input, input_args=args.input_arg,
                 substitutions=args.substitute)


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

        subparsers = self.parser.add_subparsers(help='commands')

        # CREATE BUILD IMAGE

        self.bi_parser = subparsers.add_parser('create-build-image',
                                               usage="%s [OPTIONS] create-build-image" % PROG,
                                               description='Create build image; Atomic Reactor '
                                               'installs itself inside and is capable of '
                                               'building images within this image.')
        self.bi_parser.set_defaults(func=cli_create_build_image)
        reactor_source = self.bi_parser.add_mutually_exclusive_group()
        reactor_source.add_argument("--reactor-latest", action='store_true',
                                    help="put latest Atomic Reactor inside (from public git)")
        reactor_source.add_argument("--reactor-remote-git", action='store',
                                    help="URL to git repo with Atomic Reactor (has to contain "
                                    "setup.py)")
        reactor_source.add_argument("--reactor-local-path", action='store',
                                    help="path to directory with Atomic Reactor (has to contain "
                                    "setup.py)")
        reactor_source.add_argument("--reactor-tarball-path", action='store',
                                    help="path to distribution tarball with Atomic Reactor")
        self.bi_parser.add_argument("dockerfile_dir_path", action="store",
                                    metavar="DOCKERFILE_DIR_PATH",
                                    help="path to directory with Dockerfile")
        self.bi_parser.add_argument("image", action='store', metavar="IMAGE",
                                    help="name under the image will be accessible")
        self.bi_parser.add_argument("--use-cache", action='store_true', default=False,
                                    help="use cache to build image (may be faster, but not up "
                                    "to date)")

        # inside build
        self.ib_parser = subparsers.add_parser(
            'inside-build',
            usage="%s [OPTIONS] inside-build" % PROG,
            description="build inside a container, taking build JSON input "
                        "from the source specified by the --input option.")
        self.ib_parser.add_argument("--input", action='store', default="auto",
                                    help="input plugin name (determined automatically unless "
                                    "given)")
        self.ib_parser.add_argument("--input-arg", action='append',
                                    help="argument for input plugin (in form of 'key=value'), "
                                    "see input plugins to know what arguments they accept (can "
                                    "be specified multiple times)")
        self.ib_parser.add_argument("--dont-pull-base-image", action='store_true',
                                    help="don't pull or update base image specified in "
                                    "dockerfile")
        self.ib_parser.add_argument("--substitute", action='append',
                                    help="substitute values in build json (key=value, or "
                                         "plugin_type.plugin_name.key=value)")
        self.ib_parser.set_defaults(func=cli_inside_build)


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
