"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import json
import argparse
import logging
import os
import sys
import pkg_resources

from atomic_reactor import set_logging
from atomic_reactor.api import build_image_here, build_image_in_privileged_container, build_image_using_hosts_docker
from atomic_reactor.constants import CONTAINER_BUILD_JSON_PATH, DESCRIPTION, PROG
from atomic_reactor.buildimage import BuildImageBuilder
from atomic_reactor.inner import build_inside, BuildResults
from atomic_reactor.util import process_substitutions


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


def cli_build_image(args):
    if args.plugin_files:
        args.plugin_files = [os.path.abspath(f) for f in args.plugin_files]
    if args.source__provider == 'json':
        with open(args.json_path) as json_fp:
            common_kwargs = json.load(json_fp)
        if args.substitute:
            process_substitutions(common_kwargs, args.substitute)
    else:
        common_kwargs = construct_kwargs(**vars(args))
    response = BuildResults()
    if args.method == "hostdocker":
        response = build_image_using_hosts_docker(args.build_image, **common_kwargs)
    elif args.method == "privileged":
        response = build_image_in_privileged_container(args.build_image, **common_kwargs)
    elif args.method == 'here':
        build_result = build_image_here(plugin_files=args.plugin_files, **common_kwargs)
        if build_result.is_failed():
            response.return_code = -1
        else:
            response.return_code = 0

    if response.return_code != 0:
        logger.error("build failed")
    sys.exit(response.return_code)


def construct_kwargs(**kwargs):
    ret = {}
    ret['source'] = {'provider_params': {}}

    # extend this when adding more args that should be passed to build_* functions
    recognized_kwargs = ['image', 'parent_registry', 'parent_registry_insecure',
                         'target_registries', 'target_registries_insecure', 'dont_pull_base_image']
    is_recognized_kwarg = lambda x: x in recognized_kwargs or x.startswith('source__')

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
    build_inside(input_method=args.input, input_args=args.input_arg, substitutions=args.substitute)


class CLI(object):
    def __init__(self, formatter_class=argparse.HelpFormatter, prog=PROG):
        self.parser = argparse.ArgumentParser(
            prog=prog,
            description=DESCRIPTION,
            formatter_class=formatter_class,
        )
        self.build_parser = None
        self.bi_parser = None
        self.ib_parser = None

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

        # BUILDING IMAGES

        self.build_parser = subparsers.add_parser(
            'build',
            usage="%s [OPTIONS] build" % PROG,
            description='This command enables you to build images. '
                        'Currently, you can build images from git repo, local path or '
                        'a json file with full build specification. '
                        'There are several methods for performing the build: '
                        'inside a build container using docker from host, '
                        'inside a build container using new instance of docker, '
                        'or within current environment.'
        )
        def cli_build_with_source(args):
            if args.source__provider is None:
                self.build_parser.print_help()
            else:
                cli_build_image(args)

        self.build_parser.set_defaults(func=cli_build_with_source)
        self.generate_source_types_subparsers()

        # CREATE BUILD IMAGE

        self.bi_parser = subparsers.add_parser('create-build-image',
                                               usage="%s [OPTIONS] create-build-image" % PROG,
                                               description='Create build image; Atomic Reactor installs itself inside and '
                                                           'is capable of building images within this image.')
        self.bi_parser.set_defaults(func=cli_create_build_image)
        reactor_source = self.bi_parser.add_mutually_exclusive_group()
        reactor_source.add_argument("--reactor-latest", action='store_true',
                                 help="put latest Atomic Reactor inside (from public git)")
        reactor_source.add_argument("--reactor-remote-git", action='store',
                                 help="URL to git repo with Atomic Reactor (has to contain setup.py)")
        reactor_source.add_argument("--reactor-local-path", action='store',
                                 help="path to directory with Atomic Reactor (has to contain setup.py)")
        reactor_source.add_argument("--reactor-tarball-path", action='store',
                                 help="path to distribution tarball with Atomic Reactor")
        self.bi_parser.add_argument("dockerfile_dir_path", action="store", metavar="DOCKERFILE_DIR_PATH",
                                    help="path to directory with Dockerfile")
        self.bi_parser.add_argument("image", action='store', metavar="IMAGE",
                                    help="name under the image will be accessible")
        self.bi_parser.add_argument("--use-cache", action='store_true', default=False,
                                    help="use cache to build image (may be faster, but not up to date)")

        # inside build
        self.ib_parser = subparsers.add_parser(
            'inside-build',
            usage="%s [OPTIONS] inside-build" % PROG,
            description="We do expect we are inside container, therefore we'll read "
                        "build configuration from json at '%s'" % CONTAINER_BUILD_JSON_PATH +
                        "and when the build is done, "
                        "results are written in that dir so Atomic Reactor from host may read those.")
        self.ib_parser.add_argument("--input", action='store', default="auto",
                                    help="input plugin name (determined automatically unless given)")
        self.ib_parser.add_argument("--input-arg", action='append',
                                    help="argument for input plugin (in form of 'key=value'), see input plugins "
                                         " to know what arguments they accept (can be specified multiple times)")
        self.ib_parser.add_argument("--dont-pull-base-image", action='store_true',
                                    help="don't pull or update base image specified in dockerfile")
        self.ib_parser.add_argument("--substitute", action='append',
                                    help="substitute values in build json (key=value, or "
                                         "plugin_type.plugin_name.key=value)")
        self.ib_parser.set_defaults(func=cli_inside_build)

    def generate_source_types_subparsers(self):
        build_subparsers = self.build_parser.add_subparsers(help='select source provider to use',
                                                            dest='source__provider')
        self.source_types_parsers = {
            'git': build_subparsers.add_parser(
                'git',
                help='build from a git repo',
                usage="%s [OPTIONS] build git" % PROG),
            'path': build_subparsers.add_parser(
                'path',
                help='build from a local path',
                usage="%s [OPTIONS] build path" % PROG),
        }
        source_types_uri_help = {
            'git': 'URI of the git repo',
            'path': 'URI of the directory with source (e.g. "file:///foo/bar") or\
                    path (e.g. "/foo/bar"); path can be relative',
        }
        source_types_df_path_help = {
            'git': 'path to Dockerfile within git repo (default is ./)',
            'path': 'path to Dockerfile within given directory (default is ./)',
        }

        # add arguments that are common to non-json source types
        for st, stp in self.source_types_parsers.items():
            stp.add_argument(
                "--image", action='store', required=True,
                help="name under which the image will be accessible")
            stp.add_argument(
                "--uri", action='store', metavar="URI", required=True,
                help=source_types_uri_help[st], dest='source__uri')
            stp.add_argument(
                "--df-path", action='store', help=source_types_df_path_help[st],
                dest='source__dockerfile_path')
            stp.add_argument(
                "--source-registry", action='store', metavar="REGISTRY",
                help="registry to pull base image from")
            stp.add_argument(
                "--source-registry-insecure", action='store_true',
                help="allow connecting to source registry over plain http")
            stp.add_argument(
                "--target-registries", action='store', nargs="*", metavar="REGISTRY",
                help="list of registries to push image to")
            stp.add_argument(
                "--target-registries-insecure", action='store_true',
                help="allow connecting to target registries over plain http")
            stp.add_argument(
                "--dont-pull-base-image", action='store_true',
                help="don't pull or update base image specified in dockerfile")

        # add the "json" subparser that allows to build from a json build file
        self.source_types_parsers['json'] = build_subparsers.add_parser(
            'json',
            help='load build configuration from json file',
            usage="%s [OPTIONS] build json" % PROG)

        # add arguments common to both non-json and json source types
        for stp in self.source_types_parsers.values():
            stp.add_argument(
                "--build-image", action='store',
                help="name of build image to use (build image type has to match method)")
            stp.add_argument(
                "--load-plugin", action="store", nargs="*", metavar="PLUGIN_FILE",
                dest="plugin_files", help="list of files where plugins live")
            stp.add_argument(
                "--method", action='store', required=True,
                choices=["hostdocker", "privileged", "here"],
                help="choose method for building image: "
                     "'hostdocker' mounts socket inside privileged container to use docker from host, "
                     "'privileged' runs separate docker instance inside privileged container "
                     "and 'here' executes build in current environment")

        # add sourcetype-specific arguments now
        self.source_types_parsers['git'].add_argument(
            '--commit', action='store',
            dest='source__provider_params__git_commit',
            help="checkout this commit (default is master)")
        self.source_types_parsers['json'].add_argument(
            'json_path', metavar='JSON_PATH',
            help='path to the build json')
        self.source_types_parsers['json'].add_argument(
            '--substitute', nargs='*', metavar='SUBSTITUTE',
            help='provide substitutions for json in form "foo.bar=spam"')

    def run(self):
        self.set_arguments()
        args = self.parser.parse_args()
        try:
            # Capture any warning.warn() calls and log them
            logging.captureWarnings(True)
        except AttributeError:
            # Python 2.6 doesn't have captureWarnings()
            pass

        if args.verbose:
            set_logging(level=logging.DEBUG)
        elif args.quiet:
            set_logging(level=logging.WARNING)
        else:
            set_logging(level=logging.INFO)
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
                logger.error("exception caught: %r", ex)


def run():
    cli = CLI()
    cli.run()


if __name__ == '__main__':
    run()
