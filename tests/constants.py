# -*- coding: utf-8 -*-
"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import os
MOCK = os.environ.get('NOMOCK') is None

INPUT_IMAGE = "busybox:latest"
DOCKERFILE_FILENAME = 'Dockerfile'
DOCKERFILE_GIT = "https://github.com/TomasTomecek/docker-hello-world.git"
DOCKERFILE_SHA1 = "6e592f1420efcd331cd28b360a7e02f669caf540"
DOCKERFILE_OK_PATH = os.path.join(os.path.dirname(__file__), 'files', 'docker-hello-world')
DOCKERFILE_ERROR_BUILD_PATH =\
        os.path.join(os.path.dirname(__file__), 'files', 'docker-hello-world-error-build')
DOCKERFILE_SUBDIR_PATH = os.path.join(os.path.dirname(__file__), 'files', 'df-in-subdir')
DOCKERFILE_SHA1 = "6e592f1420efcd331cd28b360a7e02f669caf540"

REGISTRY_PORT = "5000"
DOCKER0_IP = "172.17.42.1"
TEST_IMAGE = "dock-test-image"

LOCALHOST_REGISTRY = "localhost:%s" % REGISTRY_PORT
DOCKER0_REGISTRY = "%s:%s" % (DOCKER0_IP, REGISTRY_PORT)
LOCALHOST_REGISTRY_HTTP = "http://%s" % LOCALHOST_REGISTRY
DOCKER0_REGISTRY_HTTP = "http://%s" % DOCKER0_REGISTRY

COMMAND = "eporeporjgpeorjgpeorjgpeorjgpeorjgpeorjg"

NON_ASCII = "žluťoučký"
