#!/usr/bin/python
"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from setuptools import setup, find_packages

DESCRIPTION = "Python library with command line interface for building docker images."
HOMEPAGE = "https://github.com/containerbuildsystem/atomic-reactor"

setup(
    name='atomic-reactor',
    version='4.18.0',
    description=DESCRIPTION,
    author='Red Hat, Inc.',
    author_email='atomic-devel@projectatomic.io',
    url=HOMEPAGE,
    license="BSD",
    entry_points={
        'console_scripts': ['atomic-reactor=atomic_reactor.cli.main:run'],
    },
    packages=find_packages(exclude=["*.tests", "*.tests.*", "tests.*", "tests"]),
    install_requires=[
        'backoff',
        'dockerfile-parse>=0.0.13',
        'flatpak-module-tools>=0.14',
        'jsonschema',
        'paramiko>=2.10.1',
        'PyYAML',
        'ruamel.yaml',
        'osbs-client >= 1.0.0',
        'requests',
        'koji',
        'PyGObject',
        'reflink',
    ],
    python_requires='>=3.8, <4',
    package_data={'atomic_reactor': ['schemas/*.json']},
)
