#!/usr/bin/python
"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import re
import sys

from setuptools import setup, find_packages
from atomic_reactor.constants import DESCRIPTION, HOMEPAGE

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
        raise RuntimeError("Can't open file with requirements: %r", ex)
    packages = (p.strip() for p in packages if not re.match("^\s*#", p))
    packages = list(filter(None, packages))
    return packages

def _install_requirements():
    requirements = _get_requirements('requirements.txt')
    if sys.version_info[0] < 3:
        requirements += _get_requirements('requirements-py2.txt')
    return requirements

setup(
    name='atomic-reactor',
    version='1.6.8',
    description=DESCRIPTION,
    author='Red Hat, Inc.',
    author_email='atomic-devel@projectatomic.io',
    url=HOMEPAGE,
    license="BSD",
    entry_points={
        'console_scripts': ['atomic-reactor=atomic_reactor.cli.main:run',
                            'pulpsecret-gen=atomic_reactor.cli.secret:run'],
    },
    packages=find_packages(exclude=["*.tests", "*.tests.*", "tests.*", "tests"]),
    install_requires=_install_requirements(),
    data_files=data_files.items(),
)

