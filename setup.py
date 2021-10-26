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

data_files = {
    "share/atomic-reactor/images/privileged-builder": [
        "images/privileged-builder/Dockerfile",
        "images/privileged-builder/docker.sh",
    ],
    "share/atomic-reactor/images/dockerhost-builder": [
        "images/dockerhost-builder/Dockerfile",
    ],
}

setup(
    name='atomic-reactor',
    version='4.0.dev0',
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
        'docker < 4.3.0',
        'docker-squash>=1.0.7',
        'dockerfile-parse>=0.0.13',
        'flatpak-module-tools >= 0.11,<0.13;python_version<"3.9"',
        'flatpak-module-tools >= 0.11;python_version>="3.9"',
        'jsonschema',
        'PyYAML',
        'ruamel.yaml',
        'osbs-client >= 1.0.0',
        'requests',
        'PyGObject',
    ],
    python_requires='>=3.8, <4',
    package_data={'atomic_reactor': ['schemas/*.json']},
    data_files=data_files.items(),
)
