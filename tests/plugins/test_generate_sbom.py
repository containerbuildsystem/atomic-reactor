"""
Copyright (c) 2023 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from copy import deepcopy

import pytest
import yaml
from flexmock import flexmock
import koji

from tests.constants import LOCALHOST_REGISTRY
from tests.mock_env import MockEnv
from tests.utils.test_cachito import CACHITO_URL

from atomic_reactor.constants import (
    PLUGIN_FETCH_MAVEN_KEY,
    PLUGIN_RESOLVE_REMOTE_SOURCE,
    PLUGIN_RPMQA,
    INSPECT_CONFIG,
    ICM_JSON_FILENAME,
    KOJI_BTYPE_ICM,
    REPO_FETCH_ARTIFACTS_KOJI,
    REPO_FETCH_ARTIFACTS_URL,
    PLUGIN_CHECK_AND_SET_PLATFORMS_KEY,
)
from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.generate_sbom import GenerateSbomPlugin
from atomic_reactor.util import base_image_is_custom, base_image_is_scratch
from atomic_reactor.utils import retries
from osbs.utils import ImageName

pytestmark = pytest.mark.usefixtures('user_params')

KOJI_HUB = 'http://koji.localhost/hub'
KOJI_ROOT = 'http://koji.localhost/root'

REMOTE_SOURCES = [{'id': 1}, {'id': 5}, {'id': 20}]
CACHITO_SBOM_URL = '{}/api/v1/sbom?requests=1,5,20'.format(CACHITO_URL)
CACHITO_SBOM_JSON = {
    'bomFormat': 'CycloneDX',
    'specVersion': '1.4',
    'version': 1,
    'components': [
        {
          'name': 'npm-without-deps',
          'type': 'library',
          'version': '1.0.0',
          'purl': 'pkg:github/testing/npm-without-deps@2f0ce1d7b1f8b35572d919428b965285a69583f6',
        },
        {
          'name': 'fmt',
          'type': 'library',
          'purl': 'pkg:golang/fmt',
        },
        {
          'name': 'yarn-without-deps',
          'type': 'library',
          'version': '1.0.0',
          'purl': 'pkg:github/testing/yarn-without-deps@da0a2888aa7aab37fec34c0b36d9e44560d2cf3e',
        }
    ],
}

PNC_SBOM_COMPONENTS = [
    {'type': 'library', 'name': 'artifact/artifact-sha256',
     'version': '0.0.3.redhat-00003',
     'purl': 'pkg:maven/artifact/artifact-sha256@0.0.3.redhat-00003?type=jar&cl=update'},

    {'type': 'library', 'name': 'org.example.artifact/artifact-multi',
     'version': '0.0.4.redhat-00004',
     'purl': 'pkg:maven/org.example.artifact/artifact-multi@0.0.4.redhat-00004?type=jar'}
]

RPM_SBOM_COMPONENTS = {
    'x86_64': [
        {
            'type': 'library', 'name': 'vim-minimal', 'version': '9.0.803-1.fc36',
            'purl': 'pkg:rpm/vim-minimal@9.0.803-1.fc36?arch=x86_64'},

        {
            'type': 'library', 'name': 'yum', 'version': '4.14.0-1.fc36',
            'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch'},

        {
            'type': 'library', 'name': 'kernel-core', 'version': '6.0.5-200.fc36',
            'purl': 'pkg:rpm/kernel-core@6.0.5-200.fc36?arch=x86_64&epoch=3'}
    ],

    's390x': [
        {
            'type': 'library', 'name': 'vim-minimal', 'version': '9.0.803-1.fc36',
            'purl': 'pkg:rpm/vim-minimal@9.0.803-1.fc36?arch=s390x'},

        {
            'type': 'library', 'name': 'yum', 'version': '4.14.0-1.fc36',
            'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch'},

        {
            'type': 'library', 'name': 'kernel-core', 'version': '6.0.5-200.fc36',
            'purl': 'pkg:rpm/kernel-core@6.0.5-200.fc36?arch=s390x&epoch=3'}
    ]
}

FETCH_KOJI_NVR = {'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1'}
FETCH_KOJI_URL = {
    'url': 'https:/spam/spam.jar',
    'source-url': 'https:/spam/spam-sources.tar',
    'md5': 'ec61f019a3d0826c04ab20c55462aa24',
    'source-md5': '5d1ab5ae2a84b0f910a0ec549fd9e22b',
}

DEFAULT_COMPONENTS = {
    'x86_64': [
        {
            'name': 'npm-without-deps',
            'type': 'library',
            'version': '1.0.0',
            'purl': 'pkg:github/testing/npm-without-deps@2f0ce1d7b1f8b35572d919428b965285a69583f6',
            'build_dependency': False,
        },
        {
            'name': 'yarn-without-deps',
            'type': 'library',
            'version': '1.0.0',
            'purl': 'pkg:github/testing/yarn-without-deps@da0a2888aa7aab37fec34c0b36d9e44560d2cf3e',
            'build_dependency': False,
        },
        {
            'name': 'fmt',
            'type': 'library',
            'purl': 'pkg:golang/fmt',
            'build_dependency': False,
        },
        {
            'name': 'artifact/artifact-sha256',
            'type': 'library',
            'version': '0.0.3.redhat-00003',
            'purl': 'pkg:maven/artifact/artifact-sha256@0.0.3.redhat-00003?type=jar&cl=update',
            'build_dependency': False,
        },
        {
            'name': 'org.example.artifact/artifact-multi',
            'type': 'library',
            'version': '0.0.4.redhat-00004',
            'purl': 'pkg:maven/org.example.artifact/artifact-multi@0.0.4.redhat-00004?type=jar',
            'build_dependency': False,
        },
        {
            'name': 'kernel-core',
            'type': 'library',
            'version': '6.0.5-200.fc36',
            'purl': 'pkg:rpm/kernel-core@6.0.5-200.fc36?arch=x86_64&epoch=3',
            'build_dependency': False,
        },
        {
            'name': 'vim-minimal',
            'type': 'library',
            'version': '9.0.803-1.fc36',
            'purl': 'pkg:rpm/vim-minimal@9.0.803-1.fc36?arch=x86_64',
            'build_dependency': False,
        },
        {
            'name': 'yum',
            'type': 'library',
            'version': '4.14.0-1.fc36',
            'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch',
            'build_dependency': False,
        },
    ],
    's390x': [
        {
            'name': 'npm-without-deps',
            'type': 'library',
            'version': '1.0.0',
            'purl': 'pkg:github/testing/npm-without-deps@2f0ce1d7b1f8b35572d919428b965285a69583f6',
            'build_dependency': False,
        },
        {
            'name': 'yarn-without-deps',
            'type': 'library',
            'version': '1.0.0',
            'purl': 'pkg:github/testing/yarn-without-deps@da0a2888aa7aab37fec34c0b36d9e44560d2cf3e',
            'build_dependency': False,
        },
        {
            'name': 'fmt',
            'type': 'library',
            'purl': 'pkg:golang/fmt',
            'build_dependency': False,
        },
        {
            'name': 'artifact/artifact-sha256',
            'type': 'library',
            'version': '0.0.3.redhat-00003',
            'purl': 'pkg:maven/artifact/artifact-sha256@0.0.3.redhat-00003?type=jar&cl=update',
            'build_dependency': False,
        },
        {
            'name': 'org.example.artifact/artifact-multi',
            'type': 'library',
            'version': '0.0.4.redhat-00004',
            'purl': 'pkg:maven/org.example.artifact/artifact-multi@0.0.4.redhat-00004?type=jar',
            'build_dependency': False,
        },
        {
            'name': 'kernel-core',
            'type': 'library',
            'version': '6.0.5-200.fc36',
            'purl': 'pkg:rpm/kernel-core@6.0.5-200.fc36?arch=s390x&epoch=3',
            'build_dependency': False,
        },
        {
            'name': 'vim-minimal',
            'type': 'library',
            'version': '9.0.803-1.fc36',
            'purl': 'pkg:rpm/vim-minimal@9.0.803-1.fc36?arch=s390x',
            'build_dependency': False,
        },
        {
            'name': 'yum',
            'type': 'library',
            'version': '4.14.0-1.fc36',
            'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch',
            'build_dependency': False,
        },
    ],
}

DEFAULT_AND_BASE_COMPONENTS = {
    'x86_64': [
        {
            'name': 'npm-without-deps',
            'type': 'library',
            'version': '1.0.0',
            'purl': 'pkg:github/testing/npm-without-deps@2f0ce1d7b1f8b35572d919428b965285a69583f6',
            'build_dependency': False,
        },
        {
            'name': 'yarn-without-deps',
            'type': 'library',
            'version': '1.0.0',
            'purl': 'pkg:github/testing/yarn-without-deps@da0a2888aa7aab37fec34c0b36d9e44560d2cf3e',
            'build_dependency': False,
        },
        {
            'name': 'fmt',
            'type': 'library',
            'purl': 'pkg:golang/fmt',
            'build_dependency': False,
        },
        {
            'name': 'somego',
            'type': 'library',
            'purl': 'pkg:golang/somego',
            'build_dependency': False,
        },
        {
            'name': 'artifact/artifact-sha256',
            'type': 'library',
            'version': '0.0.3.redhat-00003',
            'purl': 'pkg:maven/artifact/artifact-sha256@0.0.3.redhat-00003?type=jar&cl=update',
            'build_dependency': False,
        },
        {
            'name': 'org.example.artifact/artifact-multi',
            'type': 'library',
            'version': '0.0.4.redhat-00004',
            'purl': 'pkg:maven/org.example.artifact/artifact-multi@0.0.4.redhat-00004?type=jar',
            'build_dependency': False,
        },
        {
            'name': 'kernel-core',
            'type': 'library',
            'version': '6.0.5-200.fc36',
            'purl': 'pkg:rpm/kernel-core@6.0.5-200.fc36?arch=x86_64&epoch=3',
            'build_dependency': False,
        },
        {
            'name': 'some_rpm',
            'type': 'library',
            'version': '1.0',
            'purl': 'pkg:rpm/some_rpm@1.0?arch=x86_64',
            'build_dependency': False,
        },
        {
            'name': 'vim-minimal',
            'type': 'library',
            'version': '9.0.803-1.fc36',
            'purl': 'pkg:rpm/vim-minimal@9.0.803-1.fc36?arch=x86_64',
            'build_dependency': False,
        },
        {
            'name': 'yum',
            'type': 'library',
            'version': '4.14.0-1.fc36',
            'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch',
            'build_dependency': False,
        },
    ],
    's390x': [
        {
            'name': 'npm-without-deps',
            'type': 'library',
            'version': '1.0.0',
            'purl': 'pkg:github/testing/npm-without-deps@2f0ce1d7b1f8b35572d919428b965285a69583f6',
            'build_dependency': False,
        },
        {
            'name': 'yarn-without-deps',
            'type': 'library',
            'version': '1.0.0',
            'purl': 'pkg:github/testing/yarn-without-deps@da0a2888aa7aab37fec34c0b36d9e44560d2cf3e',
            'build_dependency': False,
        },
        {
            'name': 'fmt',
            'type': 'library',
            'purl': 'pkg:golang/fmt',
            'build_dependency': False,
        },
        {
            'name': 'somego',
            'type': 'library',
            'purl': 'pkg:golang/somego',
            'build_dependency': False,
        },
        {
            'name': 'artifact/artifact-sha256',
            'type': 'library',
            'version': '0.0.3.redhat-00003',
            'purl': 'pkg:maven/artifact/artifact-sha256@0.0.3.redhat-00003?type=jar&cl=update',
            'build_dependency': False,
        },
        {
            'name': 'org.example.artifact/artifact-multi',
            'type': 'library',
            'version': '0.0.4.redhat-00004',
            'purl': 'pkg:maven/org.example.artifact/artifact-multi@0.0.4.redhat-00004?type=jar',
            'build_dependency': False,
        },
        {
            'name': 'kernel-core',
            'type': 'library',
            'version': '6.0.5-200.fc36',
            'purl': 'pkg:rpm/kernel-core@6.0.5-200.fc36?arch=s390x&epoch=3',
            'build_dependency': False,
        },
        {
            'name': 'some_rpm',
            'type': 'library',
            'version': '1.0',
            'purl': 'pkg:rpm/some_rpm@1.0?arch=s390x',
            'build_dependency': False,
        },
        {
            'name': 'vim-minimal',
            'type': 'library',
            'version': '9.0.803-1.fc36',
            'purl': 'pkg:rpm/vim-minimal@9.0.803-1.fc36?arch=s390x',
            'build_dependency': False,
        },
        {
            'name': 'yum',
            'type': 'library',
            'version': '4.14.0-1.fc36',
            'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch',
            'build_dependency': False,
        },
    ],
}

DEFAULT_AND_PARENT_COMPONENTS = {
    'x86_64': [
        {
            'name': 'npm-without-deps',
            'type': 'library',
            'version': '1.0.0',
            'purl': 'pkg:github/testing/npm-without-deps@2f0ce1d7b1f8b35572d919428b965285a69583f6',
            'build_dependency': False,
        },
        {
            'name': 'yarn-without-deps',
            'type': 'library',
            'version': '1.0.0',
            'purl': 'pkg:github/testing/yarn-without-deps@da0a2888aa7aab37fec34c0b36d9e44560d2cf3e',
            'build_dependency': False,
        },
        {
            'name': 'fmt',
            'type': 'library',
            'purl': 'pkg:golang/fmt',
            'build_dependency': False,
        },
        {
            'name': 'artifact/artifact-sha256',
            'type': 'library',
            'version': '0.0.3.redhat-00003',
            'purl': 'pkg:maven/artifact/artifact-sha256@0.0.3.redhat-00003?type=jar&cl=update',
            'build_dependency': False,
        },
        {
            'name': 'org.example.artifact/artifact-multi',
            'type': 'library',
            'version': '0.0.4.redhat-00004',
            'purl': 'pkg:maven/org.example.artifact/artifact-multi@0.0.4.redhat-00004?type=jar',
            'build_dependency': False,
        },
        {
            'name': 'kernel-core',
            'type': 'library',
            'version': '6.0.5-200.fc36',
            'purl': 'pkg:rpm/kernel-core@6.0.5-200.fc36?arch=x86_64&epoch=3',
            'build_dependency': False,
        },
        {
            'name': 'vim-minimal',
            'type': 'library',
            'version': '9.0.803-1.fc36',
            'purl': 'pkg:rpm/vim-minimal@9.0.803-1.fc36?arch=x86_64',
            'build_dependency': False,
        },
        {
            'name': 'yum',
            'type': 'library',
            'version': '4.14.0-1.fc36',
            'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch',
            'build_dependency': False,
        },
        {
            'name': 'parentgo',
            'type': 'library',
            'purl': 'pkg:golang/parentgo',
            'build_dependency': True,
        },
        {
            'name': 'parent_rpm',
            'type': 'library',
            'version': '1.0',
            'purl': 'pkg:rpm/parent_rpm@1.0?arch=x86_64',
            'build_dependency': True,
        },
        {
            'name': 'yum',
            'type': 'library',
            'version': '4.14.0-1.fc36',
            'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch',
            'build_dependency': True,
        },
    ],
    's390x': [
        {
            'name': 'npm-without-deps',
            'type': 'library',
            'version': '1.0.0',
            'purl': 'pkg:github/testing/npm-without-deps@2f0ce1d7b1f8b35572d919428b965285a69583f6',
            'build_dependency': False,
        },
        {
            'name': 'yarn-without-deps',
            'type': 'library',
            'version': '1.0.0',
            'purl': 'pkg:github/testing/yarn-without-deps@da0a2888aa7aab37fec34c0b36d9e44560d2cf3e',
            'build_dependency': False,
        },
        {
            'name': 'fmt',
            'type': 'library',
            'purl': 'pkg:golang/fmt',
            'build_dependency': False,
        },
        {
            'name': 'artifact/artifact-sha256',
            'type': 'library',
            'version': '0.0.3.redhat-00003',
            'purl': 'pkg:maven/artifact/artifact-sha256@0.0.3.redhat-00003?type=jar&cl=update',
            'build_dependency': False,
        },
        {
            'name': 'org.example.artifact/artifact-multi',
            'type': 'library',
            'version': '0.0.4.redhat-00004',
            'purl': 'pkg:maven/org.example.artifact/artifact-multi@0.0.4.redhat-00004?type=jar',
            'build_dependency': False,
        },
        {
            'name': 'kernel-core',
            'type': 'library',
            'version': '6.0.5-200.fc36',
            'purl': 'pkg:rpm/kernel-core@6.0.5-200.fc36?arch=s390x&epoch=3',
            'build_dependency': False,
        },
        {
            'name': 'vim-minimal',
            'type': 'library',
            'version': '9.0.803-1.fc36',
            'purl': 'pkg:rpm/vim-minimal@9.0.803-1.fc36?arch=s390x',
            'build_dependency': False,
        },
        {
            'name': 'yum',
            'type': 'library',
            'version': '4.14.0-1.fc36',
            'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch',
            'build_dependency': False,
        },
        {
            'name': 'parentgo',
            'type': 'library',
            'purl': 'pkg:golang/parentgo',
            'build_dependency': True,
        },
        {
            'name': 'parent_rpm',
            'type': 'library',
            'version': '1.0',
            'purl': 'pkg:rpm/parent_rpm@1.0?arch=s390x',
            'build_dependency': True,
        },
        {
            'name': 'yum',
            'type': 'library',
            'version': '4.14.0-1.fc36',
            'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch',
            'build_dependency': True,
        },
    ],
}

DEFAULT_AND_BASE_AND_PARENT_COMPONENTS = {
    'x86_64': [
        {
            'name': 'npm-without-deps',
            'type': 'library',
            'version': '1.0.0',
            'purl': 'pkg:github/testing/npm-without-deps@2f0ce1d7b1f8b35572d919428b965285a69583f6',
            'build_dependency': False,
        },
        {
            'name': 'yarn-without-deps',
            'type': 'library',
            'version': '1.0.0',
            'purl': 'pkg:github/testing/yarn-without-deps@da0a2888aa7aab37fec34c0b36d9e44560d2cf3e',
            'build_dependency': False,
        },
        {
            'name': 'fmt',
            'type': 'library',
            'purl': 'pkg:golang/fmt',
            'build_dependency': False,
        },
        {
            'name': 'somego',
            'type': 'library',
            'purl': 'pkg:golang/somego',
            'build_dependency': False,
        },
        {
            'name': 'artifact/artifact-sha256',
            'type': 'library',
            'version': '0.0.3.redhat-00003',
            'purl': 'pkg:maven/artifact/artifact-sha256@0.0.3.redhat-00003?type=jar&cl=update',
            'build_dependency': False,
        },
        {
            'name': 'org.example.artifact/artifact-multi',
            'type': 'library',
            'version': '0.0.4.redhat-00004',
            'purl': 'pkg:maven/org.example.artifact/artifact-multi@0.0.4.redhat-00004?type=jar',
            'build_dependency': False,
        },
        {
            'type': 'library',
            'name': 'kernel-core',
            'version': '6.0.5-200.fc36',
            'purl': 'pkg:rpm/kernel-core@6.0.5-200.fc36?arch=x86_64&epoch=3',
            'build_dependency': False,
        },
        {
            'name': 'some_rpm',
            'type': 'library',
            'version': '1.0',
            'purl': 'pkg:rpm/some_rpm@1.0?arch=x86_64',
            'build_dependency': False,
        },
        {
            'name': 'vim-minimal',
            'type': 'library',
            'version': '9.0.803-1.fc36',
            'purl': 'pkg:rpm/vim-minimal@9.0.803-1.fc36?arch=x86_64',
            'build_dependency': False,
        },
        {
            'type': 'library',
            'name': 'yum',
            'version': '4.14.0-1.fc36',
            'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch',
            'build_dependency': False,
        },
        {
            'name': 'parentgo',
            'type': 'library',
            'purl': 'pkg:golang/parentgo',
            'build_dependency': True,
        },
        {
            'name': 'parent_rpm',
            'type': 'library',
            'version': '1.0',
            'purl': 'pkg:rpm/parent_rpm@1.0?arch=x86_64',
            'build_dependency': True,
        },
        {
            'type': 'library',
            'name': 'yum',
            'version': '4.14.0-1.fc36',
            'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch',
            'build_dependency': True,
        },
    ],
    's390x': [
        {
            'name': 'npm-without-deps',
            'type': 'library',
            'version': '1.0.0',
            'purl': 'pkg:github/testing/npm-without-deps@2f0ce1d7b1f8b35572d919428b965285a69583f6',
            'build_dependency': False,
        },
        {
            'name': 'yarn-without-deps',
            'type': 'library',
            'version': '1.0.0',
            'purl': 'pkg:github/testing/yarn-without-deps@da0a2888aa7aab37fec34c0b36d9e44560d2cf3e',
            'build_dependency': False,
        },
        {
            'name': 'fmt',
            'type': 'library',
            'purl': 'pkg:golang/fmt',
            'build_dependency': False,
        },
        {
            'name': 'somego',
            'type': 'library',
            'purl': 'pkg:golang/somego',
            'build_dependency': False,
        },
        {
            'name': 'artifact/artifact-sha256',
            'type': 'library',
            'version': '0.0.3.redhat-00003',
            'purl': 'pkg:maven/artifact/artifact-sha256@0.0.3.redhat-00003?type=jar&cl=update',
            'build_dependency': False,
        },
        {
            'name': 'org.example.artifact/artifact-multi',
            'type': 'library',
            'version': '0.0.4.redhat-00004',
            'purl': 'pkg:maven/org.example.artifact/artifact-multi@0.0.4.redhat-00004?type=jar',
            'build_dependency': False,
        },
        {
            'type': 'library',
            'name': 'kernel-core',
            'version': '6.0.5-200.fc36',
            'purl': 'pkg:rpm/kernel-core@6.0.5-200.fc36?arch=s390x&epoch=3',
            'build_dependency': False,
        },
        {
            'name': 'some_rpm',
            'type': 'library',
            'version': '1.0',
            'purl': 'pkg:rpm/some_rpm@1.0?arch=s390x',
            'build_dependency': False,
        },
        {
            'name': 'vim-minimal',
            'type': 'library',
            'version': '9.0.803-1.fc36',
            'purl': 'pkg:rpm/vim-minimal@9.0.803-1.fc36?arch=s390x',
            'build_dependency': False,
        },
        {
            'type': 'library',
            'name': 'yum',
            'version': '4.14.0-1.fc36',
            'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch',
            'build_dependency': False,
        },
        {
            'name': 'parentgo',
            'type': 'library',
            'purl': 'pkg:golang/parentgo',
            'build_dependency': True,
        },
        {
            'name': 'parent_rpm',
            'type': 'library',
            'version': '1.0',
            'purl': 'pkg:rpm/parent_rpm@1.0?arch=s390x',
            'build_dependency': True,
        },
        {
            'type': 'library',
            'name': 'yum',
            'version': '4.14.0-1.fc36',
            'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch',
            'build_dependency': True,
        },
    ],
}

# image is missing required label
MISSING_LABEL_IMAGE_NAME = 'registry/missing_labels_image:latest'
MISSING_LABEL_IMAGE_LABELS = {
    'com.redhat.component': 'missing_labels_image',
}

# brew build doesn't have valid json, but None
NOJSON_SBOM_IMAGE_NAME = 'registry/nojson_sbom_image:latest'
NOJSON_SBOM_IMAGE_LABELS = {
    'com.redhat.component': 'nojson_sbom_image',
    'version': '1.0',
    'release': '1',
}
NOJSON_SBOM_BUILD_NVR = f"{NOJSON_SBOM_IMAGE_LABELS['com.redhat.component']}-" \
                        f"{NOJSON_SBOM_IMAGE_LABELS['version']}-" \
                        f"{NOJSON_SBOM_IMAGE_LABELS['release']}"
NOJSON_SBOM_KOJI_BUILD = {
    'nvr': NOJSON_SBOM_BUILD_NVR,
    'build_id': 1,
    'state': 1,
    'name': NOJSON_SBOM_IMAGE_LABELS['com.redhat.component'],
    'version': NOJSON_SBOM_IMAGE_LABELS['version'],
    'release': NOJSON_SBOM_IMAGE_LABELS['release'],
}

# sbom without any components
EMPTY_SBOM_IMAGE_NAME = 'registry/empty_sbom_image:latest'
EMPTY_SBOM_IMAGE_LABELS = {
    'com.redhat.component': 'empty_sbom_image',
    'version': '1.0',
    'release': '1',
}
EMPTY_SBOM_BUILD_NVR = f"{EMPTY_SBOM_IMAGE_LABELS['com.redhat.component']}-" \
                       f"{EMPTY_SBOM_IMAGE_LABELS['version']}-" \
                       f"{EMPTY_SBOM_IMAGE_LABELS['release']}"
EMPTY_SBOM_KOJI_BUILD = {
    'nvr': EMPTY_SBOM_BUILD_NVR,
    'build_id': 1,
    'state': 1,
    'name': EMPTY_SBOM_IMAGE_LABELS['com.redhat.component'],
    'version': EMPTY_SBOM_IMAGE_LABELS['version'],
    'release': EMPTY_SBOM_IMAGE_LABELS['release'],
}
EMPTY_SBOM_JSON = {
    'bomFormat': 'CycloneDX',
    'specVersion': '1.4',
    'version': 1,
    'components': [],
}
EMPTY_SBOM_BUILD_SBOM_JSON = {
    'x86_64': deepcopy(EMPTY_SBOM_JSON),
    's390x': deepcopy(EMPTY_SBOM_JSON),
}

# base image with sbom with some components
BASE_WITH_SBOM_IMAGE_NAME = 'registry/base_with_sbom_image:latest'
BASE_WITH_SBOM_IMAGE_LABELS = {
    'com.redhat.component': 'base_with_sbom_image',
    'version': '1.0',
    'release': '1',
}
BASE_WITH_SBOM_BUILD_NVR = f"{BASE_WITH_SBOM_IMAGE_LABELS['com.redhat.component']}-" \
                           f"{BASE_WITH_SBOM_IMAGE_LABELS['version']}-" \
                           f"{BASE_WITH_SBOM_IMAGE_LABELS['release']}"
BASE_WITH_SBOM_KOJI_BUILD = {
    'nvr': BASE_WITH_SBOM_BUILD_NVR,
    'build_id': 1,
    'state': 1,
    'name': BASE_WITH_SBOM_IMAGE_LABELS['com.redhat.component'],
    'version': BASE_WITH_SBOM_IMAGE_LABELS['version'],
    'release': BASE_WITH_SBOM_IMAGE_LABELS['release'],
}
BASE_WITH_SBOM_BUILD_SBOM_JSON = {
    'x86_64': {
        'bomFormat': 'CycloneDX',
        'specVersion': '1.4',
        'version': 1,
        'components': [
            {
                'name': 'somego',
                'type': 'library',
                'purl': 'pkg:golang/somego',
                'build_dependency': True,
            },
            {
                'name': 'some_rpm',
                'type': 'library',
                'version': '1.0',
                'purl': 'pkg:rpm/some_rpm@1.0?arch=x86_64',
                'build_dependency': False,
            },
            # same component as in default components
            {
                'name': 'yum',
                'type': 'library',
                'version': '4.14.0-1.fc36',
                'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch',
                'build_dependency': False,
            },
        ],
    },
    's390x': {
        'bomFormat': 'CycloneDX',
        'specVersion': '1.4',
        'version': 1,
        'components': [
            {
                'name': 'somego',
                'type': 'library',
                'purl': 'pkg:golang/somego',
                'build_dependency': True,
            },
            {
                'name': 'some_rpm',
                'type': 'library',
                'version': '1.0',
                'purl': 'pkg:rpm/some_rpm@1.0?arch=s390x',
                'build_dependency': False,
            },
            # same component as in default components
            {
                'name': 'yum',
                'type': 'library',
                'version': '4.14.0-1.fc36',
                'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch',
                'build_dependency': False,
            },
        ],
    },
}

# base image with sbom with some components and incompleteness reasons
BASE_WITH_SBOM_INC_IMAGE_NAME = 'registry/base_with_sbom_inc_image:latest'
BASE_WITH_SBOM_INC_IMAGE_LABELS = {
    'com.redhat.component': 'base_with_sbom_inc_image',
    'version': '1.0',
    'release': '1',
}
BASE_WITH_SBOM_INC_BUILD_NVR = f"{BASE_WITH_SBOM_INC_IMAGE_LABELS['com.redhat.component']}-" \
                               f"{BASE_WITH_SBOM_INC_IMAGE_LABELS['version']}-" \
                               f"{BASE_WITH_SBOM_INC_IMAGE_LABELS['release']}"
BASE_WITH_SBOM_INC_KOJI_BUILD = {
    'nvr': BASE_WITH_SBOM_INC_BUILD_NVR,
    'build_id': 1,
    'state': 1,
    'name': BASE_WITH_SBOM_INC_IMAGE_LABELS['com.redhat.component'],
    'version': BASE_WITH_SBOM_INC_IMAGE_LABELS['version'],
    'release': BASE_WITH_SBOM_INC_IMAGE_LABELS['release'],
}
BASE_WITH_SBOM_INC_BUILD_SBOM_JSON = deepcopy(BASE_WITH_SBOM_BUILD_SBOM_JSON)
BASE_WITH_SBOM_INC_BUILD_SBOM_JSON['x86_64']['incompleteness_reasons'] = [
    {'type': 'other', 'description': 'fetch koji is used'},
    {'type': 'other', 'description': 'fetch url is used'},
]
BASE_WITH_SBOM_INC_BUILD_SBOM_JSON['s390x']['incompleteness_reasons'] = [
    {'type': 'other', 'description': 'fetch koji is used'},
    {'type': 'other', 'description': 'fetch url is used'},
]

# parent image without koji build
PARENT_WITHOUT_KOJI_IMAGE_NAME = 'registry/parent_without_koji_image:latest'
PARENT_WITHOUT_KOJI_IMAGE_LABELS = {
    'com.redhat.component': 'parent_without_koji_image',
    'version': '1.0',
    'release': '1',
}
PARENT_WITHOUT_KOJI_BUILD_NVR = f"{PARENT_WITHOUT_KOJI_IMAGE_LABELS['com.redhat.component']}-" \
                                f"{PARENT_WITHOUT_KOJI_IMAGE_LABELS['version']}-" \
                                f"{PARENT_WITHOUT_KOJI_IMAGE_LABELS['release']}"

# parent image without sbom
PARENT_WITHOUT_SBOM_IMAGE_NAME = 'registry/parent_without_sbom_image:latest'
PARENT_WITHOUT_SBOM_IMAGE_LABELS = {
    'com.redhat.component': 'parent_without_sbom_image',
    'version': '1.0',
    'release': '1',
}
PARENT_WITHOUT_SBOM_BUILD_NVR = f"{PARENT_WITHOUT_SBOM_IMAGE_LABELS['com.redhat.component']}-" \
                                f"{PARENT_WITHOUT_SBOM_IMAGE_LABELS['version']}-" \
                                f"{PARENT_WITHOUT_SBOM_IMAGE_LABELS['release']}"
PARENT_WITHOUT_SBOM_KOJI_BUILD = {
    'nvr': PARENT_WITHOUT_SBOM_BUILD_NVR,
    'build_id': 2,
    'state': 1,
    'name': PARENT_WITHOUT_SBOM_IMAGE_LABELS['com.redhat.component'],
    'version': PARENT_WITHOUT_SBOM_IMAGE_LABELS['version'],
    'release': PARENT_WITHOUT_SBOM_IMAGE_LABELS['release'],
}

# parent image with sbom with some components
PARENT_WITH_SBOM_IMAGE_NAME = 'registry/parent_with_sbom_image:latest'
PARENT_WITH_SBOM_IMAGE_LABELS = {
    'com.redhat.component': 'parent_with_sbom_image',
    'version': '1.0',
    'release': '1',
}
PARENT_WITH_SBOM_BUILD_NVR = f"{PARENT_WITH_SBOM_IMAGE_LABELS['com.redhat.component']}-" \
                             f"{PARENT_WITH_SBOM_IMAGE_LABELS['version']}-" \
                             f"{PARENT_WITH_SBOM_IMAGE_LABELS['release']}"
PARENT_WITH_SBOM_KOJI_BUILD = {
    'nvr': PARENT_WITH_SBOM_BUILD_NVR,
    'build_id': 1,
    'state': 1,
    'name': PARENT_WITH_SBOM_IMAGE_LABELS['com.redhat.component'],
    'version': PARENT_WITH_SBOM_IMAGE_LABELS['version'],
    'release': PARENT_WITH_SBOM_IMAGE_LABELS['release'],
}
PARENT_WITH_SBOM_BUILD_SBOM_JSON = {
    'x86_64': {
        'bomFormat': 'CycloneDX',
        'specVersion': '1.4',
        'version': 1,
        'components': [
            {
                'name': 'parentgo',
                'type': 'library',
                'purl': 'pkg:golang/parentgo',
                'build_dependency': False,
            },
            {
                'name': 'parent_rpm',
                'type': 'library',
                'version': '1.0',
                'purl': 'pkg:rpm/parent_rpm@1.0?arch=x86_64',
                'build_dependency': True,
            },
            # same component as in default components
            {
                'name': 'yum',
                'type': 'library',
                'version': '4.14.0-1.fc36',
                'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch',
                'build_dependency': True,
            },
        ],
    },
    's390x': {
        'bomFormat': 'CycloneDX',
        'specVersion': '1.4',
        'version': 1,
        'components': [
            {
                'name': 'parentgo',
                'type': 'library',
                'purl': 'pkg:golang/parentgo',
                'build_dependency': False,
            },
            {
                'name': 'parent_rpm',
                'type': 'library',
                'version': '1.0',
                'purl': 'pkg:rpm/parent_rpm@1.0?arch=s390x',
                'build_dependency': True,
            },
            # same component as in default components
            {
                'name': 'yum',
                'type': 'library',
                'version': '4.14.0-1.fc36',
                'purl': 'pkg:rpm/yum@4.14.0-1.fc36?arch=noarch',
                'build_dependency': True,
            },
        ],
    },
}

# parent image with sbom with some components and incompleteness reasons
PARENT_WITH_SBOM_INC_IMAGE_NAME = 'registry/parent_with_sbom_inc_image:latest'
PARENT_WITH_SBOM_INC_IMAGE_LABELS = {
    'com.redhat.component': 'parent_with_sbom_inc_image',
    'version': '1.0',
    'release': '1',
}
PARENT_WITH_SBOM_INC_BUILD_NVR = f"{PARENT_WITH_SBOM_INC_IMAGE_LABELS['com.redhat.component']}-" \
                                 f"{PARENT_WITH_SBOM_INC_IMAGE_LABELS['version']}-" \
                                 f"{PARENT_WITH_SBOM_INC_IMAGE_LABELS['release']}"
PARENT_WITH_SBOM_INC_KOJI_BUILD = {
    'nvr': PARENT_WITH_SBOM_INC_BUILD_NVR,
    'build_id': 1,
    'state': 1,
    'name': PARENT_WITH_SBOM_INC_IMAGE_LABELS['com.redhat.component'],
    'version': PARENT_WITH_SBOM_INC_IMAGE_LABELS['version'],
    'release': PARENT_WITH_SBOM_INC_IMAGE_LABELS['release'],
}
PARENT_WITH_SBOM_INC_BUILD_SBOM_JSON = deepcopy(PARENT_WITH_SBOM_BUILD_SBOM_JSON)
PARENT_WITH_SBOM_INC_BUILD_SBOM_JSON['x86_64']['incompleteness_reasons'] = [
    {'type': 'other', 'description': 'lookaside cache is used'},
]
PARENT_WITH_SBOM_INC_BUILD_SBOM_JSON['s390x']['incompleteness_reasons'] = [
    {'type': 'other', 'description': 'lookaside cache is used'},
]

# parent imagge in building state
BUILDING_IMAGE_NAME = 'registry/building_image:latest'
BUILDING_IMAGE_LABELS = {
    'com.redhat.component': 'building_image',
    'version': '1.0',
    'release': '1',
}
BUILDING_BUILD_NVR = f"{BUILDING_IMAGE_LABELS['com.redhat.component']}-" \
                     f"{BUILDING_IMAGE_LABELS['version']}-" \
                     f"{BUILDING_IMAGE_LABELS['release']}"
BUILDING_KOJI_BUILD = {
    'nvr': BUILDING_BUILD_NVR,
    'build_id': 1,
    'state': 0,
    'name': BUILDING_IMAGE_LABELS['com.redhat.component'],
    'version': BUILDING_IMAGE_LABELS['version'],
    'release': BUILDING_IMAGE_LABELS['release'],
}

INCOMPLETE_CACHE_URL_KOJI = [
    {'type': 'other', 'description': 'fetch koji is used'},
    {'type': 'other', 'description': 'fetch url is used'},
    {'type': 'other', 'description': 'lookaside cache is used'},
]
INCOMPLETE_BUILDING = [
    {'type': 'other', 'description': f"parent build '{BUILDING_BUILD_NVR}' is missing SBOM"},
]
INCOMPLETE_MISSING_LABEL = [
    {'type': 'other', 'description': 'parent build is missing SBOM'},
]
INCOMPLETE_MISSING_SBOM = [
    {'type': 'other', 'description':
        f"parent build '{PARENT_WITHOUT_SBOM_BUILD_NVR}' is missing SBOM"},
]
INCOMPLETE_MISSING_KOJI = [
    {'type': 'other', 'description':
        f"parent build '{PARENT_WITHOUT_KOJI_BUILD_NVR}' is missing SBOM"},
]
PLATFORMS = ['x86_64', 's390x']
UNIQUE_IMAGE = f'{LOCALHOST_REGISTRY}/namespace/some_image:1.0'


def setup_function(*args):
    # IMPORTANT: This needs to be done to ensure mocks at the module
    # level are reset between test cases.
    sys.modules.pop(GenerateSbomPlugin.key, None)


def teardown_function(*args):
    # IMPORTANT: This needs to be done to ensure mocks at the module
    # level are reset between test cases.
    sys.modules.pop(GenerateSbomPlugin.key, None)


def mock_env(workflow, df_images):
    tmp_dir = tempfile.mkdtemp()
    dockerconfig_contents = {"auths": {LOCALHOST_REGISTRY: {"username": "user",
                                                            "email": "test@example.com",
                                                            "password": "mypassword"}}}
    with open(os.path.join(tmp_dir, ".dockerconfigjson"), "w") as f:
        f.write(json.dumps(dockerconfig_contents))
        f.flush()

    r_c_m = {
        'version': 1,
        'koji': {
            'hub_url': KOJI_HUB,
            'root_url': KOJI_ROOT,
            'auth': {
                'ssl_certs_dir': ''
            },
        },
        'cachito': {
            'api_url': CACHITO_URL,
            'auth': {
                'ssl_certs_dir': ''
            },
        },
        'source_registry': {
            'url': 'registry',
        },
        'registries_cfg_path': tmp_dir,
    }

    env = (MockEnv(workflow)
           .for_plugin(GenerateSbomPlugin.key)
           .set_reactor_config(r_c_m)
           .set_dockerfile_images(df_images)
           .set_plugin_result(PLUGIN_RESOLVE_REMOTE_SOURCE, deepcopy(REMOTE_SOURCES))
           .set_plugin_result(PLUGIN_FETCH_MAVEN_KEY,
                              {'sbom_components': deepcopy(PNC_SBOM_COMPONENTS)})
           .set_plugin_result(PLUGIN_RPMQA, deepcopy(RPM_SBOM_COMPONENTS))
           )

    all_inspects = [(EMPTY_SBOM_IMAGE_LABELS, EMPTY_SBOM_IMAGE_NAME),
                    (MISSING_LABEL_IMAGE_LABELS, MISSING_LABEL_IMAGE_NAME),
                    (BUILDING_IMAGE_LABELS, BUILDING_IMAGE_NAME),
                    (BASE_WITH_SBOM_IMAGE_LABELS, BASE_WITH_SBOM_IMAGE_NAME),
                    (BASE_WITH_SBOM_INC_IMAGE_LABELS, BASE_WITH_SBOM_INC_IMAGE_NAME),
                    (PARENT_WITH_SBOM_IMAGE_LABELS, PARENT_WITH_SBOM_IMAGE_NAME),
                    (PARENT_WITH_SBOM_INC_IMAGE_LABELS, PARENT_WITH_SBOM_INC_IMAGE_NAME),
                    (PARENT_WITHOUT_SBOM_IMAGE_LABELS, PARENT_WITHOUT_SBOM_IMAGE_NAME),
                    (PARENT_WITHOUT_KOJI_IMAGE_LABELS, PARENT_WITHOUT_KOJI_IMAGE_NAME),
                    (NOJSON_SBOM_IMAGE_LABELS, NOJSON_SBOM_IMAGE_NAME)]

    for labels, imagename in all_inspects:
        inspect = {INSPECT_CONFIG: {'Labels': labels.copy()}}
        (flexmock(workflow.imageutil)
         .should_receive('get_inspect_for_image')
         .with_args(image=ImageName.parse(imagename))
         .and_return(inspect))

    workflow.data.plugins_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = PLATFORMS
    return env.create_runner()


def mock_get_sbom_cachito(requests_mock):
    requests_mock.register_uri('GET', CACHITO_SBOM_URL, json=CACHITO_SBOM_JSON)


def mock_build_icm_urls(requests_mock):
    all_sboms = [(EMPTY_SBOM_KOJI_BUILD, EMPTY_SBOM_BUILD_SBOM_JSON),
                 (BASE_WITH_SBOM_KOJI_BUILD, BASE_WITH_SBOM_BUILD_SBOM_JSON),
                 (BASE_WITH_SBOM_INC_KOJI_BUILD, BASE_WITH_SBOM_INC_BUILD_SBOM_JSON),
                 (PARENT_WITH_SBOM_KOJI_BUILD, PARENT_WITH_SBOM_BUILD_SBOM_JSON),
                 (PARENT_WITH_SBOM_INC_KOJI_BUILD, PARENT_WITH_SBOM_INC_BUILD_SBOM_JSON)]

    for platform in PLATFORMS:
        for build, sbom_json in all_sboms:
            sbom_url = get_build_icm_url(build, platform)
            requests_mock.register_uri('GET', sbom_url,
                                       json=sbom_json[platform])

        # sbom isn't valid json
        sbom_url = get_build_icm_url(NOJSON_SBOM_KOJI_BUILD, platform)
        requests_mock.register_uri('GET', sbom_url, json=None)


def get_build_icm_url(koji_build, platform):
    base = '{}/packages/{}/{}/{}'.format(KOJI_ROOT, koji_build['name'], koji_build['version'],
                                         koji_build['release'])
    return '{}/files/{}/{}'.format(base, KOJI_BTYPE_ICM, ICM_JSON_FILENAME.format(platform))


@pytest.fixture()
def koji_session():
    session = flexmock()
    flexmock(session).should_receive('ssl_login').and_return(True)
    flexmock(session).should_receive('krb_login').and_return(True)

    all_builds = [(EMPTY_SBOM_BUILD_NVR, EMPTY_SBOM_KOJI_BUILD),
                  (BUILDING_BUILD_NVR, BUILDING_KOJI_BUILD),
                  (BASE_WITH_SBOM_BUILD_NVR, BASE_WITH_SBOM_KOJI_BUILD),
                  (BASE_WITH_SBOM_INC_BUILD_NVR, BASE_WITH_SBOM_INC_KOJI_BUILD),
                  (PARENT_WITH_SBOM_BUILD_NVR, PARENT_WITH_SBOM_KOJI_BUILD),
                  (PARENT_WITH_SBOM_INC_BUILD_NVR, PARENT_WITH_SBOM_INC_KOJI_BUILD),
                  (PARENT_WITHOUT_SBOM_BUILD_NVR, PARENT_WITHOUT_SBOM_KOJI_BUILD),
                  (PARENT_WITHOUT_KOJI_BUILD_NVR, None),
                  (NOJSON_SBOM_BUILD_NVR, NOJSON_SBOM_KOJI_BUILD)]

    for build_nvr, koji_build in all_builds:
        (flexmock(session)
         .should_receive('getBuild')
         .with_args(build_nvr)
         .and_return(koji_build))

    icm_archives = []
    for platform in PLATFORMS:
        icm_archives.append({'id': 1, 'type_name': 'json',
                             'filename': ICM_JSON_FILENAME.format(platform)})
    (flexmock(session)
     .should_receive('listArchives')
     .with_args(1, type=KOJI_BTYPE_ICM)
     .and_return(icm_archives))

    (flexmock(session)
     .should_receive('listArchives')
     .with_args(2, type=KOJI_BTYPE_ICM)
     .and_return([]))

    flexmock(koji).should_receive('ClientSession').and_return(session)
    return session


@pytest.mark.parametrize(
    ('df_images, use_cache, use_fetch_url, use_fetch_koji, expected_components,'
     'expected_incomplete'), [
        (
            ['scratch'],
            False,
            False,
            False,
            DEFAULT_COMPONENTS,
            [],
        ),
        (
            ['scratch', 'koji/image-build'],
            False,
            False,
            False,
            DEFAULT_COMPONENTS,
            [],
        ),
        (
            ['scratch'],
            True,
            True,
            True,
            DEFAULT_COMPONENTS,
            INCOMPLETE_CACHE_URL_KOJI,
        ),
        (
            ['scratch', 'koji/image-build'],
            True,
            True,
            True,
            DEFAULT_COMPONENTS,
            INCOMPLETE_CACHE_URL_KOJI,
        ),
        (
            [EMPTY_SBOM_IMAGE_NAME],
            False,
            False,
            False,
            DEFAULT_COMPONENTS,
            [],
        ),
        (
            [PARENT_WITHOUT_SBOM_IMAGE_NAME],
            False,
            False,
            False,
            DEFAULT_COMPONENTS,
            INCOMPLETE_MISSING_SBOM,
        ),
        (
            [PARENT_WITHOUT_KOJI_IMAGE_NAME],
            False,
            False,
            False,
            DEFAULT_COMPONENTS,
            INCOMPLETE_MISSING_KOJI,
        ),
        (
            [MISSING_LABEL_IMAGE_NAME],
            False,
            False,
            False,
            DEFAULT_COMPONENTS,
            INCOMPLETE_MISSING_LABEL,
        ),
        (
            [BUILDING_IMAGE_NAME],
            False,
            False,
            False,
            DEFAULT_COMPONENTS,
            INCOMPLETE_BUILDING,
        ),
        (
            [BASE_WITH_SBOM_IMAGE_NAME],
            False,
            False,
            False,
            DEFAULT_AND_BASE_COMPONENTS,
            [],
        ),
        (
            [PARENT_WITH_SBOM_IMAGE_NAME, 'scratch'],
            False,
            False,
            False,
            DEFAULT_AND_PARENT_COMPONENTS,
            [],
        ),
        (
            [PARENT_WITH_SBOM_IMAGE_NAME, 'scratch', BASE_WITH_SBOM_IMAGE_NAME],
            False,
            False,
            False,
            DEFAULT_AND_BASE_AND_PARENT_COMPONENTS,
            [],
        ),
        (
            [PARENT_WITH_SBOM_IMAGE_NAME, 'scratch', BASE_WITH_SBOM_IMAGE_NAME],
            True,
            True,
            True,
            DEFAULT_AND_BASE_AND_PARENT_COMPONENTS,
            INCOMPLETE_CACHE_URL_KOJI,
        ),
        (
            [PARENT_WITH_SBOM_INC_IMAGE_NAME, 'scratch', BASE_WITH_SBOM_INC_IMAGE_NAME],
            False,
            False,
            False,
            DEFAULT_AND_BASE_AND_PARENT_COMPONENTS,
            INCOMPLETE_CACHE_URL_KOJI,
        ),
    ])
def test_sbom(workflow, requests_mock, koji_session, df_images, use_cache, use_fetch_url,
              use_fetch_koji, expected_components, expected_incomplete):
    mock_get_sbom_cachito(requests_mock)
    mock_build_icm_urls(requests_mock)

    runner = mock_env(workflow, df_images)
    workflow.data.tag_conf.add_unique_image(UNIQUE_IMAGE)

    def check_cosign_run(args):
        matches = False
        for platform in PLATFORMS:
            exp_cmd = ['cosign', 'attach', 'sbom',
                       f'{UNIQUE_IMAGE}-{platform}', '--type=cyclonedx']
            exp_sbom = f'icm-{platform}.json'

            if exp_cmd == args[:-1]:
                assert args[-1].startswith('--sbom=')

                if args[-1].endswith(exp_sbom):
                    matches = True

        assert matches
        return ''

    (flexmock(retries)
     .should_receive('run_cmd')
     .times(len(PLATFORMS))
     .replace_with(check_cosign_run))

    source_path = Path(workflow.source.path)

    if use_cache:
        source_path.joinpath('sources').write_text('#ref file.tar.gz', 'utf-8')
    else:
        source_path.joinpath('sources').touch()

    if use_fetch_koji:
        nvrs = [FETCH_KOJI_NVR]
        source_path.joinpath(REPO_FETCH_ARTIFACTS_KOJI).write_text(yaml.safe_dump(nvrs), 'utf-8')

    if use_fetch_url:
        urls = [FETCH_KOJI_URL]
        source_path.joinpath(REPO_FETCH_ARTIFACTS_URL).write_text(yaml.safe_dump(urls), 'utf-8')

    for image in df_images:
        if not (base_image_is_scratch(image) or base_image_is_custom(image)):
            workflow.data.dockerfile_images[image] = image

    all_results = runner.run()
    plugin_result = all_results[GenerateSbomPlugin.key]

    expected_sbom = deepcopy(EMPTY_SBOM_JSON)
    expected_sbom['incompleteness_reasons'] = expected_incomplete
    expected_result = {plat: deepcopy(expected_sbom) for plat in PLATFORMS}

    for plat in PLATFORMS:
        expected_result[plat]['components'].extend(deepcopy(expected_components[plat]))

    assert plugin_result == expected_result


@pytest.mark.parametrize(('df_images, err_msg'), [
    ([NOJSON_SBOM_IMAGE_NAME], 'JSON data is expected from'),
])
def test_sbom_raises(workflow, requests_mock, koji_session, df_images, err_msg):
    mock_get_sbom_cachito(requests_mock)
    mock_build_icm_urls(requests_mock)

    runner = mock_env(workflow, df_images)

    for image in df_images:
        if not (base_image_is_scratch(image) or base_image_is_custom(image)):
            workflow.data.dockerfile_images[image] = image

    with pytest.raises(PluginFailedException) as exc:
        runner.run()

    assert err_msg in str(exc.value)


@pytest.mark.parametrize(('df_images, err_msg'), [
    (['scratch'], f'SBOM push for platform {PLATFORMS[0]} failed with output:'),
])
def test_sbom_raises_cosign(workflow, requests_mock, koji_session, df_images, err_msg, caplog):
    mock_get_sbom_cachito(requests_mock)
    mock_build_icm_urls(requests_mock)

    runner = mock_env(workflow, df_images)
    workflow.data.tag_conf.add_unique_image(UNIQUE_IMAGE)

    (flexmock(retries)
     .should_receive('run_cmd')
     .times(1)
     .and_raise(subprocess.CalledProcessError(1, 'cosign', output=b'something went wrong')))

    with pytest.raises(PluginFailedException):
        runner.run()

    assert err_msg in caplog.text
