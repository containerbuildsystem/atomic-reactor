#!/usr/bin/python
"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import re

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


def _get_requirements(path):
    try:
        with open(path) as f:
            packages = f.read().splitlines()
    except (IOError, OSError) as ex:
        raise RuntimeError("Can't open file with requirements: %s", ex)
    packages = (p.strip() for p in packages if not re.match(r'^\s*#', p))
    packages = list(filter(None, packages))
    return packages


setup(
    name='atomic-reactor',
    version='3.12.0',
    description=DESCRIPTION,
    author='Red Hat, Inc.',
    author_email='atomic-devel@projectatomic.io',
    url=HOMEPAGE,
    license="BSD",
    entry_points={
        'console_scripts': ['atomic-reactor=atomic_reactor.cli.main:run'],
    },
    packages=find_packages(exclude=["*.tests", "*.tests.*", "tests.*", "tests"]),
    install_requires=_get_requirements('requirements.txt'),
    python_requires='>=3.6, <4',
    package_data={'atomic_reactor': ['schemas/*.json']},
    data_files=data_files.items(),
)
