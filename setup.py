#!/usr/bin/python

from setuptools import setup, find_packages

setup(name='dock',
      version='0.0.1',
      description='improved builder for docker images',
      author='Tomas Tomecek',
      author_email='ttomecek@redhat.com',
      url='https://github.com/DBuildService/dock',
      entry_points={
          'console_scripts': ['dock=dock.cli.main:run'],
      },
      packages=find_packages(),
)

