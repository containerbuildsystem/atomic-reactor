"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import os
from sys import version_info

# from python-six
PY2 = version_info[0] == 2

DOCKER_SOCKET_PATH = '/var/run/docker.sock'
DOCKERFILE_FILENAME = 'Dockerfile'
BUILD_JSON = 'build.json'
BUILD_JSON_ENV = 'BUILD_JSON'
RESULTS_JSON = 'results.json'

CONTAINER_SHARE_PATH = '/run/share/'
CONTAINER_SHARE_SOURCE_SUBDIR = 'source'
CONTAINER_SECRET_PATH = ''
CONTAINER_BUILD_JSON_PATH = os.path.join(CONTAINER_SHARE_PATH, BUILD_JSON)
CONTAINER_RESULTS_JSON_PATH = os.path.join(CONTAINER_SHARE_PATH, RESULTS_JSON)
CONTAINER_DOCKERFILE_PATH = os.path.join(CONTAINER_SHARE_PATH, DOCKERFILE_FILENAME)

HOST_SECRET_PATH = ''

EXPORTED_SQUASHED_IMAGE_NAME = 'image.tar'

YUM_REPOS_DIR = '/etc/yum.repos.d/'
RELATIVE_REPOS_PATH = "atomic-reactor-repos/"
DEFAULT_YUM_REPOFILE_NAME = 'atomic-reactor-injected.repo'

SOURCE_DIRECTORY_NAME = "source"

# docs constants

DESCRIPTION = "Python library with command line interface for building docker images."
HOMEPAGE = "https://github.com/DBuildService/atomic-reactor"
PROG = "atomic-reactor"
MANPAGE_AUTHORS = "Jiri Popelka <jpopelka@redhat.com>, " \
                  "Martin Milata <mmilata@redhat.com>, " \
                  "Slavek Kabrda <slavek@redhat.com>, " \
                  "Tim Waugh <twaug@redhat.com>, " \
                  "Tomas Tomecek <ttomecek@redhat.com>"
MANPAGE_SECTION = 1
