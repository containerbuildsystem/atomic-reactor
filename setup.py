#!/usr/bin/python

import re

from setuptools import setup, find_packages

data_files = {
    "/usr/share/dock/images/privileged-builder": [
        "images/privileged-builder/Dockerfile",
        "images/privileged-builder/docker.sh",
    ],
    "/usr/share/dock/images/dockerhost-builder": [
        "images/dockerhost-builder/Dockerfile",
    ],
}

def _get_requirements(path):
    try:
        with open(path) as f:
            packages = f.read().splitlines()
    except (IOError, OSError) as ex:
        raise RuntimeError("Can't open file with requirements: %s", repr(ex))
    packages = (p.strip() for p in packages if not re.match("^\s*#", p))
    packages = list(filter(None, packages))
    return packages

def _install_requirements():
    requirements = _get_requirements('requirements.txt')
    return requirements

setup(name='dock',
      version='1.0.0.b',
      description='improved builder for docker images',
      author='Tomas Tomecek',
      author_email='ttomecek@redhat.com',
      url='https://github.com/DBuildService/dock',
      entry_points={
          'console_scripts': ['dock=dock.cli.main:run'],
      },
      packages=find_packages(exclude=["tests"]),
      install_requires=_install_requirements(),
      data_files=data_files.items(),
)

