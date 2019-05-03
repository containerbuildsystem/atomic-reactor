"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals, absolute_import

import pytest
from flexmock import flexmock

from atomic_reactor.auth import HTTPRegistryAuth
from atomic_reactor.constants import PLUGIN_GROUP_MANIFESTS_KEY
from atomic_reactor.util import ImageName, ManifestDigest
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow, DockerRegistry
from atomic_reactor.plugin import ExitPluginsRunner, PluginFailedException
from atomic_reactor.plugins.exit_delete_from_registry import DeleteFromRegistryPlugin
from atomic_reactor.plugins.build_orchestrate_build import OrchestrateBuildPlugin
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from tests.constants import LOCALHOST_REGISTRY, DOCKER0_REGISTRY, MOCK, TEST_IMAGE, INPUT_IMAGE

from tempfile import mkdtemp
import os
import json
import requests

if MOCK:
    from tests.docker_mock import mock_docker
    from tests.retry_mock import mock_get_retry_session

DIGEST1 = 'sha256:28b64a8b29fd2723703bb17acf907cd66898440270e536992b937899a4647414'
DIGEST2 = 'sha256:0000000000000000000000000000000000000000000000000000000000000000'
DIGEST_LIST = 'sha256:deadbeef'


class Y(object):
    def __init__(self):
        self.dockerfile_path = None
        self.path = None


class X(object):
    image_id = INPUT_IMAGE
    source = Y()
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
@pytest.mark.parametrize("orchestrator", [True, False])
@pytest.mark.parametrize("manifest_list_digests", [
    {},
    {'foo/bar': ManifestDigest(v2_list=DIGEST_LIST)}
])
def test_delete_from_registry_plugin(saved_digests, req_registries, tmpdir, orchestrator,
                                     manifest_list_digests, reactor_config_map):
    if MOCK:
        mock_docker()
        mock_get_retry_session()

    buildstep_plugin = None
    if orchestrator:
        ann_digests = []
        buildstep_plugin = [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                'platforms': "x86_64"
            },
        }]

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow({"provider": "git", "uri": "asd"}, TEST_IMAGE,
                                   buildstep_plugins=buildstep_plugin, )
    setattr(workflow, 'builder', X)

    args_registries = {}
    config_map_regiestries = []
    for reg, use_secret in req_registries.items():
        cm_reg = {'url': reg}
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
                args_registries[reg] = {'secret': temp_dir}
                cm_reg['auth'] = {'cfg_path': temp_dir}
        else:
            args_registries[reg] = {}
        config_map_regiestries.append(cm_reg)

    for reg, digests in saved_digests.items():
        if orchestrator:
            for tag, dig in digests.items():
                repo = tag.split(':')[0]
                t = tag.split(':')[1]
                ann_digests.append({
                    'digest': dig,
                    'tag': t,
                    'repository': repo,
                    'registry': reg,
                })
        else:
            r = DockerRegistry(reg)
            for tag, dig in digests.items():
                r.digests[tag] = ManifestDigest(v1='not-used', v2=dig)
            workflow.push_conf._registries['docker'].append(r)

    group_manifest_digests = {}
    if orchestrator:
        build_annotations = {'digests': ann_digests}
        annotations = {'worker-builds': {'x86_64': build_annotations}}
        setattr(workflow, 'build_result', Y)
        setattr(workflow.build_result, 'annotations', annotations)

        # group_manifest digest should be added only
        # if there are worker builds and images are pushed to one registry
        if len(req_registries) == 1 and len(saved_digests.keys()) == 1 and \
           all(saved_digests.values()):
            workflow.postbuild_results[PLUGIN_GROUP_MANIFESTS_KEY] = manifest_list_digests
            for ml_repo, ml_digest in manifest_list_digests.items():
                for reg in req_registries:
                    if reg in saved_digests:
                        group_manifest_digests.setdefault(reg, {})
                        group_manifest_digests[reg] = saved_digests[reg].copy()
                        group_manifest_digests[reg][ml_repo] = ml_digest.default

    result_digests = saved_digests.copy()
    result_digests.update(group_manifest_digests)

    if reactor_config_map:
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig({'version': 1, 'registries': config_map_regiestries})

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
    for reg, digests in result_digests.items():
        if reg not in req_registries:
            continue

        for tag, dig in digests.items():
            if dig in deleted_digests:
                continue
            url = "https://" + reg + "/v2/" + tag.split(":")[0] + "/manifests/" + dig
            auth_type = HTTPRegistryAuth
            (flexmock(requests.Session)
                .should_receive('delete')
                .with_args(url, verify=bool, auth=auth_type)
                .once()
                .and_return(flexmock(status_code=202, ok=True, raise_for_status=lambda: None)))
            deleted_digests.add(dig)

    result = runner.run()
    assert result[DeleteFromRegistryPlugin.key] == deleted_digests


@pytest.mark.parametrize("status_code", [requests.codes.ACCEPTED,
                                         requests.codes.NOT_FOUND,
                                         requests.codes.METHOD_NOT_ALLOWED,
                                         520])
def test_delete_from_registry_failures(tmpdir, status_code, reactor_config_map):
    if MOCK:
        mock_docker()
        mock_get_retry_session()

    req_registries = {DOCKER0_REGISTRY: True}
    saved_digests = {DOCKER0_REGISTRY: {'foo/bar:latest': DIGEST1}}

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow({"provider": "git", "uri": "asd"}, TEST_IMAGE)
    setattr(workflow, 'builder', X)

    args_registries = {}
    config_map_regiestries = []
    for reg, use_secret in req_registries.items():
        cm_reg = {'url': reg}
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
                args_registries[reg] = {'secret': temp_dir}
                cm_reg['auth'] = {'cfg_path': temp_dir}
        else:
            args_registries[reg] = {}
    config_map_regiestries.append(cm_reg)

    for reg, digests in saved_digests.items():
        r = DockerRegistry(reg)
        for tag, dig in digests.items():
            r.digests[tag] = ManifestDigest(v1='not-used', v2=dig)
        workflow.push_conf._registries['docker'].append(r)

    if reactor_config_map:
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig({'version': 1, 'registries': config_map_regiestries})

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
            auth_type = HTTPRegistryAuth

            response = requests.Response()
            response.status_code = status_code

            (flexmock(requests.Session)
                .should_receive('delete')
                .with_args(url, verify=bool, auth=auth_type)
                .and_return(response))

            deleted_digests.add(dig)

    if status_code == 520:
        with pytest.raises(PluginFailedException):
            result = runner.run()
            assert result[DeleteFromRegistryPlugin.key] == set([])
    else:
        result = runner.run()

        if status_code == requests.codes.ACCEPTED:
            assert result[DeleteFromRegistryPlugin.key] == deleted_digests
        else:
            assert result[DeleteFromRegistryPlugin.key] == set([])
