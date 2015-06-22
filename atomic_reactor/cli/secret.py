"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import json
import argparse
import pkg_resources
from base64 import b64encode


def generate_json(args):
    with open(args.cert, "r") as fpcer:
        with open(args.key, "r") as fpkey:
            pulpsecret = {
                'apiVersion': 'v1beta3',
                'kind': 'Secret',
                'metadata': {
                    'name': args.name,
                    'namespace': args.namespace
                },
                'data': {
                    'pulp.cer': b64encode(fpcer.read()),
                    'pulp.key': b64encode(fpkey.read())
        }
    }
    print(json.dumps(pulpsecret, indent=2))


class CLI(object):
    def __init__(self):
        self.parser = argparse.ArgumentParser(
            description="pulpsecret-gen, tool for creating secret resource"
        )

    def set_arguments(self):
        try:
            version = pkg_resources.get_distribution("atomic_reactor").version
        except pkg_resources.DistributionNotFound:
            version = "GIT"

        exclusive_group = self.parser.add_mutually_exclusive_group()
        exclusive_group.add_argument("-V", "--version", action="version", version=version)
        self.parser.add_argument('-C', '--cert', default=False, required=True,
                                 help='specify a certificate file')
        self.parser.add_argument('-K', '--key', default=False, required=True,
                                 help='specify a key file')
        self.parser.add_argument('--name', default='pulpsecret',
                                 help='resource name')
        self.parser.add_argument('--namespace', default='default',
                                 help='namespace')

    def run(self):
        self.set_arguments()
        args = self.parser.parse_args()
        try:
            generate_json(args)
        except KeyboardInterrupt:
            pass


def run():
    cli = CLI()
    cli.run()


if __name__ == '__main__':
    run()
