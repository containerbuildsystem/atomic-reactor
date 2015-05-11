"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import os
MOCK = os.environ.get('NOMOCK') is None

INPUT_IMAGE = "busybox:latest"
DOCKERFILE_GIT = "https://github.com/TomasTomecek/docker-hello-world.git"

REGISTRY_PORT = "5000"
DOCKER0_IP = "172.17.42.1"
TEST_IMAGE = "dock-test-image"

LOCALHOST_REGISTRY = "localhost:%s" % REGISTRY_PORT
DOCKER0_REGISTRY = "%s:%s" % (DOCKER0_IP, REGISTRY_PORT)
LOCALHOST_REGISTRY_HTTP = "http://%s" % LOCALHOST_REGISTRY
DOCKER0_REGISTRY_HTTP = "http://%s" % DOCKER0_REGISTRY

COMMAND = "eporeporjgpeorjgpeorjgpeorjgpeorjgpeorjg"
