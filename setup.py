#!/usr/bin/python

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

setup(name='dock',
      version='1.0.0.a',
      description='improved builder for docker images',
      author='Tomas Tomecek',
      author_email='ttomecek@redhat.com',
      url='https://github.com/DBuildService/dock',
      entry_points={
          'console_scripts': ['dock=dock.cli.main:run'],
      },
      packages=find_packages(exclude=["tests"]),
      data_files=data_files.items(),
)

