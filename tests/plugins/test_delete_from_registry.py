"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import pytest
from flexmock import flexmock

from atomic_reactor.util import ImageName, ManifestDigest
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow, DockerRegistry
from atomic_reactor.plugin import ExitPluginsRunner
from atomic_reactor.plugins.exit_delete_from_registry import DeleteFromRegistryPlugin
from tests.constants import LOCALHOST_REGISTRY, DOCKER0_REGISTRY, MOCK, TEST_IMAGE, INPUT_IMAGE

from tempfile import mkdtemp
import os
import json
import requests
import requests.auth

if MOCK:
    from tests.docker_mock import mock_docker

DIGEST1 = 'sha256:28b64a8b29fd2723703bb17acf907cd66898440270e536992b937899a4647414'
DIGEST2 = 'sha256:0000000000000000000000000000000000000000000000000000000000000000'

class Y(object):
    pass

class X(object):
    image_id = INPUT_IMAGE
    source = Y()
    source.dockerfile_path = None
    source.path = None
    base_image = ImageName(repo="qwe", tag="asd")

@pytest.mark.parametrize("saved_digests", [
    {},
    {DOCKER0_REGISTRY: {}},
    {DOCKER0_REGISTRY: {'foo/bar:latest': DIGEST1}},
    {DOCKER0_REGISTRY: {'foo/bar:latest': DIGEST1, 'foo/bar:1.0-1': DIGEST1}},
    {DOCKER0_REGISTRY: {'foo/bar:1.0-1': DIGEST1, 'foo/bar:1.0': DIGEST2}},
    {DOCKER0_REGISTRY: {'foo/bar:1.0-1': DIGEST1}, LOCALHOST_REGISTRY: {'foo/bar:1.0-1': DIGEST2}},
])
@pytest.mark.parametrize("req_registries", [
    {},
    {LOCALHOST_REGISTRY: True},
    {DOCKER0_REGISTRY: False},
    {DOCKER0_REGISTRY: True, LOCALHOST_REGISTRY: True},
    {DOCKER0_REGISTRY: False, LOCALHOST_REGISTRY: True},
])
def test_delete_from_registry_plugin(saved_digests, req_registries, tmpdir):
    if MOCK:
        mock_docker()

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow({"provider": "git", "uri": "asd"}, TEST_IMAGE)
    setattr(workflow, 'builder', X)

    args_registries = {}
    for reg, use_secret in req_registries.items():
        if use_secret:
            temp_dir = mkdtemp(dir=str(tmpdir))
            with open(os.path.join(temp_dir, ".dockercfg"), "w+") as dockerconfig:
                dockerconfig_contents = {
                    reg: {
                        "username": "user", "password": reg
                    }
                }
                dockerconfig.write(json.dumps(dockerconfig_contents))
                dockerconfig.flush()
                args_registries[reg] = { 'secret': temp_dir }
        else:
            args_registries[reg] = {}

    for reg, digests in saved_digests.items():
        r = DockerRegistry(reg)
        for tag, dig in digests.items():
            r.digests[tag] = ManifestDigest(v1='not-used', v2=dig)
        workflow.push_conf._registries['docker'].append(r)

    runner = ExitPluginsRunner(
        tasker,
        workflow,
        [{
            'name': DeleteFromRegistryPlugin.key,
            'args': {
                'registries': args_registries
            },
        }]
    )

    deleted_digests = set()
    for reg, digests in saved_digests.items():
        if reg not in req_registries:
            continue

        for tag, dig in digests.items():
            if dig in deleted_digests:
                continue
            url = "https://" + reg + "/v2/" + tag.split(":")[0] + "/manifests/" + dig
            auth_type = requests.auth.HTTPBasicAuth if req_registries[reg] else None
            (flexmock(requests)
                .should_receive('delete')
                .with_args(url, verify=bool, auth=auth_type)
                .once()
                .and_return(flexmock(status_code=202)))
            deleted_digests.add(dig)

    result = runner.run()
    assert result[DeleteFromRegistryPlugin.key] == deleted_digests
