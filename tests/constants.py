# -*- coding: utf-8 -*-
"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import os
from textwrap import dedent

HERE = os.path.dirname(__file__)
FILES = os.path.join(HERE, 'files')

MOCK = os.environ.get('NOMOCK') is None

INPUT_IMAGE = "busybox:latest"
DOCKERFILE_FILENAME = 'Dockerfile'
DOCKERFILE_GIT = "https://github.com/TomasTomecek/docker-hello-world.git"
DOCKERFILE_SHA1 = "6e592f1420efcd331cd28b360a7e02f669caf540"
DOCKERFILE_OK_PATH = os.path.join(FILES, 'docker-hello-world')
DOCKERFILE_MULTISTAGE_PATH = os.path.join(FILES, 'docker-hello-world-multistage')
DOCKERFILE_ERROR_BUILD_PATH = os.path.join(FILES, 'docker-hello-world-error-build')
SOURCE_CONFIG_ERROR_PATH = os.path.join(FILES, 'docker-hello-world-error-config')
DOCKERFILE_SUBDIR_PATH = os.path.join(FILES, 'df-in-subdir')

FLATPAK_GIT = "git://pkgs.fedoraproject.org/modules/eog.git"
FLATPAK_SHA1 = "603bb298c8fb60936590e159b7a6387d6e090a09"

SOURCE = {
    'provider': 'git',
    'uri': DOCKERFILE_GIT,
    'provider_params': {
        'git_commit': 'master',
    }
}

MOCK_SOURCE = {'provider': 'git', 'uri': 'asd'}

REGISTRY_PORT = "5000"
DOCKER0_IP = "172.17.42.1"
TEST_IMAGE = "atomic-reactor-test-image"

LOCALHOST_REGISTRY = "localhost:%s" % REGISTRY_PORT
DOCKER0_REGISTRY = "%s:%s" % (DOCKER0_IP, REGISTRY_PORT)
LOCALHOST_REGISTRY_HTTP = "http://%s" % LOCALHOST_REGISTRY
DOCKER0_REGISTRY_HTTP = "http://%s" % DOCKER0_REGISTRY

COMMAND = "eporeporjgpeorjgpeorjgpeorjgpeorjgpeorjg"

IMPORTED_IMAGE_ID = 'eee28534d167d7b3297eace1fc32c46aabedc40696e48ae04c7654f974700cc2'

REACTOR_CONFIG_MAP = dedent("""\
version: 1
koji:
    hub_url: https://koji.example.com/hub
    root_url: https://koji.example.com/root
    auth:
        proxyuser: proxyuser
        krb_principal: krb_principal
        krb_keytab_path: /tmp/krb_keytab

pulp:
    name: my-pulp
    auth:
        password: testpasswd
        username: testuser

odcs:
    api_url: https://odcs.example.com/api/1
    auth:
        ssl_certs_dir: /var/run/secrets/atomic-reactor/odcssecret
    insecure: True
    signing_intents:
    - name: release
      keys: [R123]
    - name: beta
      keys: [R123, B456]
    - name: unsigned
      keys: []
    default_signing_intent: default

smtp:
    host: smtp.example.com
    from_address: osbs@example.com
    error_addresses:
    - support@example.com
    domain: example.com
    send_to_submitter: True
    send_to_pkg_owner: True

pdc:
    api_url: https://pdc.example.com/rest_api/v1
    insecure: True

arrangement_version: 6

artifacts_allowed_domains:
- download.example.com/released
- download.example.com/candidates

image_labels:
    vendor: "Spam Inc."
    authoritative-source-url: registry.public.example.com
    distribution-scope: public

image_label_info_url_format: "https://catalog.example.com/{com.redhat.component}:{name}-{version}"

image_equal_labels:
- [description, io.k8s.description]

openshift:
    url: https://openshift.example.com
    auth:
        enable: True
        ssl_certs_dir: /var/run/secrets/atomic-reactor/odcssecret
    insecure: True
    build_json_dir: /usr/share/osbs/

group_manifests: False

platform_descriptors:
- platform: x86_64
  architecture: amd64
  enable_v1: True

content_versions:
- v1
- v2

registries:
- url: https://container-registry.example.com/v2
  auth:
      cfg_path: /var/run/secrets/atomic-reactor/v2-registry-dockercfg

source_registry:
    url: https://registry.private.example.com
    insecure: True

sources_command: "fedpkg sources"

required_secrets:
- kojisecret
- pulpsecret
- odcssecret
- v2-registry-dockercfg

worker_token_secrets:
- x86-64-worker-1
- x86-64-worker-2

prefer_schema1_digest: True

yum_proxy: http://proxy.example.com

clusters:
  foo:
   - name: blah
     max_concurrent_builds: 1
""")
