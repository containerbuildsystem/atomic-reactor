"""
Copyright (c) 2015-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import platform
import random
import time
from copy import deepcopy
from datetime import datetime

import koji as koji
import osbs
import pytest
from osbs.utils import ImageName

from atomic_reactor.constants import (
    IMAGE_TYPE_OCI,
    PLUGIN_CHECK_AND_SET_PLATFORMS_KEY,
    PLUGIN_FLATPAK_CREATE_OCI,
    PLUGIN_SOURCE_CONTAINER_KEY,
)
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.tag_and_push import TagAndPushPlugin
from atomic_reactor.plugins.fetch_sources import PLUGIN_FETCH_SOURCES_KEY
from atomic_reactor.utils import retries
from tests.constants import (LOCALHOST_REGISTRY, TEST_IMAGE, TEST_IMAGE_NAME, MOCK,
                             DOCKER0_REGISTRY)
from tests.util import add_koji_map_in_workflow

import json
import os.path
from tempfile import mkdtemp
import requests
import subprocess
from base64 import b64encode

if MOCK:
    from flexmock import flexmock
    from tests.retry_mock import mock_get_retry_session

DIGEST_V1 = 'sha256:7de72140ec27a911d3f88d60335f08d6530a4af136f7beab47797a196e840afd'
DIGEST_V2 = 'sha256:85a7e3fb684787b86e64808c5b91d926afda9d6b35a0642a72d7a746452e71c1'
DIGEST_OCI = 'sha256:bb57e66a2dabcd59a721639b67bafb6d8aa35fbe0939d39a51b087b4504718e0'

DIGEST_LOG = 'sha256:hey-this-should-not-be-used'
IMAGE_METADATA_DOCKER_ARCHIVE = {'path': '/dir/x86_64/image.tar',
                                 'type': 'docker-archive',
                                 'size': 10240,
                                 'md5sum': 'faaa',
                                 'sha256sum': '70cb91'}
IMAGE_METADATA_OCI = {'path': '/dir/x86_64/image.tar',
                      'type': 'oci',
                      'size': 10240,
                      'md5sum': 'faaa',
                      'sha256sum': '70cb91'}


def get_repositories_annotations(tag_conf):
    primary_repositories = []
    for image in tag_conf.primary_images:
        primary_repositories.append(image.to_str())

    unique_repositories = []
    for image in tag_conf.unique_images:
        unique_repositories.append(image.to_str())

    floating_repositories = []
    for image in tag_conf.floating_images:
        floating_repositories.append(image.to_str())

    return {
        "primary": primary_repositories,
        "unique": unique_repositories,
        "floating": floating_repositories,
    }


@pytest.mark.parametrize("use_secret", [
    True,
    False,
])
@pytest.mark.parametrize(("image_name", "should_raise", "missing_v2"), [
    (TEST_IMAGE_NAME, False, False),
    (TEST_IMAGE_NAME, False, True),
    (DOCKER0_REGISTRY + '/' + TEST_IMAGE_NAME, True, False),
    (TEST_IMAGE_NAME, True, False),
])
@pytest.mark.parametrize(("file_name", "dockerconfig_contents"), [
    (".dockercfg", {LOCALHOST_REGISTRY: {"email": "test@example.com",
                                         "auth": b64encode(b'user:mypassword').decode('utf-8')}}),
    (".dockercfg", {LOCALHOST_REGISTRY: {"username": "user",
                                         "email": "test@example.com",
                                         "password": "mypassword"}}),
    (".dockerconfigjson", {"auths": {LOCALHOST_REGISTRY: {"username": "user",
                                                          "email": "test@example.com",
                                                          "password": "mypassword"}}}),
])
def test_tag_and_push_plugin(
        workflow, monkeypatch, caplog,
        image_name, should_raise, missing_v2,
        use_secret, file_name, dockerconfig_contents):
    workflow.user_params['flatpak'] = True
    platforms = ['x86_64', 'ppc64le', 's390x', 'aarch64']
    workflow.data.tag_conf.add_unique_image(ImageName.parse(image_name))
    workflow.data.plugins_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = platforms
    workflow.build_dir.init_build_dirs(platforms, workflow.source)

    secret_path = None
    if use_secret:
        temp_dir = mkdtemp()
        with open(os.path.join(temp_dir, file_name), "w+") as dockerconfig:
            dockerconfig.write(json.dumps(dockerconfig_contents))
            dockerconfig.flush()
            secret_path = temp_dir

    # Add a mock OCI image to 'flatpak_create_oci' results; this forces the tag_and_push
    # plugin to push with skopeo

    workflow.data.plugins_results[PLUGIN_FLATPAK_CREATE_OCI] = {}

    # Since we are always mocking the push for now, we can get away with a stub image
    for current_platform in platforms:
        metadata = deepcopy(IMAGE_METADATA_OCI)
        metadata['ref_name'] = f'app/org.gnome.eog/{current_platform}/master'
        workflow.data.plugins_results[PLUGIN_FLATPAK_CREATE_OCI][current_platform] = metadata

    manifest_latest_url = "https://{}/v2/{}/manifests/latest".format(LOCALHOST_REGISTRY, TEST_IMAGE)
    manifest_url = "https://{}/v2/{}/manifests/{}".format(LOCALHOST_REGISTRY, TEST_IMAGE, DIGEST_V2)

    # We return our v2 manifest in the mocked v1 response as a placeholder - only the
    # digest matters anyways
    manifest_response_v1 = requests.Response()
    (flexmock(manifest_response_v1,
              status_code=200,
              headers={
                'Content-Type': 'application/vnd.docker.distribution.manifest.v1+json',
                'Docker-Content-Digest': DIGEST_V1
              }))

    manifest_response_v2 = requests.Response()
    (flexmock(manifest_response_v2,
              status_code=200,
              headers={
                'Content-Type': 'application/vnd.docker.distribution.manifest.v2+json',
                'Docker-Content-Digest': DIGEST_V2
              }))
    manifest_response_v2_list = requests.Response()
    (flexmock(manifest_response_v2_list,
              raise_for_status=lambda: None,
              headers={
                'Content-Type': 'application/vnd.docker.distribution.manifest.list.v2+json',
              }))
    if should_raise:
        (flexmock(retries)
         .should_receive('run_cmd')
         .and_raise(subprocess.CalledProcessError(1, 'echo', output=b'something went wrong')))
    else:
        (flexmock(retries)
         .should_receive('run_cmd')
         .and_return(0))

    manifest_unknown_response = requests.Response()
    (flexmock(manifest_unknown_response,
              status_code=404,
              json={
                  "errors": [{"code": "MANIFEST_UNKNOWN"}]
              }))

    def custom_get(method, url, headers, **kwargs):
        if url.startswith(manifest_latest_url):
            # For a manifest stored as v2 or v1, the docker registry defaults to
            # returning a v1 manifest if a v2 manifest is not explicitly requested
            if headers['Accept'] == 'application/vnd.docker.distribution.manifest.v2+json':
                if missing_v2:
                    return manifest_unknown_response
                else:
                    return manifest_response_v2
            elif headers['Accept'] == 'application/vnd.docker.distribution.manifest.list.v2+json':
                return manifest_response_v2_list
            else:
                return manifest_response_v1

        if url == manifest_url:
            if missing_v2:
                return manifest_unknown_response
            else:
                return manifest_response_v2

    mock_get_retry_session()
    (flexmock(requests.Session)
        .should_receive('request')
        .replace_with(custom_get))
    (flexmock(time)
        .should_receive('sleep')
        .and_return(None))

    rcm = {'version': 1,
           'registries': [{'url': LOCALHOST_REGISTRY,
                           'insecure': True,
                           'auth': {'cfg_path': secret_path}}]}
    workflow.conf.conf = rcm
    add_koji_map_in_workflow(workflow, hub_url='', root_url='')

    runner = PostBuildPluginsRunner(
        workflow,
        [{
            'name': TagAndPushPlugin.key,
            'args': {},
        }]
    )

    if should_raise:
        with pytest.raises(PluginFailedException):
            runner.run()
    else:
        runner.run()
        assert workflow.conf.registry
        repos_annotations = get_repositories_annotations(workflow.data.tag_conf)
        assert workflow.data.annotations['repositories'] == repos_annotations

        if MOCK:
            # we only test this when mocking docker because we don't expect
            # running actual docker against v2 registry
            if missing_v2:
                assert "Retrying push because V2 schema 2" in caplog.text


@pytest.mark.parametrize(("is_source_build", "v2s2", "unsupported_image_type"), [
    (True, True, False),
    (True, False, False),
    (False, True, True),
    (False, False, False),
    (False, True, False),
    (False, False, True),
])
@pytest.mark.parametrize("use_secret", [
    True,
    False,
])
@pytest.mark.parametrize("fail_push", [
    False,
    True,
])
def test_tag_and_push_plugin_oci(workflow, monkeypatch, is_source_build, v2s2,
                                 unsupported_image_type, use_secret, fail_push, caplog):
    sources_koji_id = '123456'
    sources_koji_target = 'source_target'
    sources_koji_repo = 'namespace/container_build_image'
    sources_koji_pull_spec = 'registry_url/{}@sha256:987654321'.format(sources_koji_repo)
    sources_random_number = 1234567890
    sources_timestamp = datetime(year=2019, month=12, day=12)
    current_platform = platform.processor() or 'x86_64'
    sources_tagname = '{}-{}-{}-{}'.format(sources_koji_target, sources_random_number,
                                           sources_timestamp.strftime('%Y%m%d%H%M%S'),
                                           current_platform)

    wf_data = workflow.data
    if is_source_build:
        wf_data.plugins_results[PLUGIN_FETCH_SOURCES_KEY] = {
            'sources_for_koji_build_id': sources_koji_id
        }
        platforms = ['x86_64']
        workflow.build_dir.init_build_dirs(platforms, workflow.source)
        image_metadata = deepcopy(IMAGE_METADATA_DOCKER_ARCHIVE)
        wf_data.plugins_results[PLUGIN_SOURCE_CONTAINER_KEY] = {'image_metadata': image_metadata}
    else:
        wf_data.tag_conf.add_unique_image(f'{LOCALHOST_REGISTRY}/{TEST_IMAGE}')
        workflow.user_params['flatpak'] = True
        platforms = ['x86_64', 'ppc64le', 's390x', 'aarch64']
        wf_data.plugins_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = platforms
        workflow.build_dir.init_build_dirs(platforms, workflow.source)

    class MockedClientSession(object):
        def __init__(self, hub, opts=None):
            pass

        def getBuild(self, build_info):
            if is_source_build:
                assert build_info == sources_koji_id
                return {'extra': {'image': {'index': {'pull': [sources_koji_pull_spec]}}}}

            else:
                return None

        def krb_login(self, *args, **kwargs):
            return True

    session = MockedClientSession('')
    flexmock(koji, ClientSession=session)
    flexmock(random).should_receive('randrange').and_return(sources_random_number)
    flexmock(osbs.utils).should_receive('utcnow').and_return(sources_timestamp)

    secret_path = None
    if use_secret:
        temp_dir = mkdtemp()
        with open(os.path.join(temp_dir, ".dockercfg"), "w+") as dockerconfig:
            dockerconfig_contents = {
                LOCALHOST_REGISTRY: {
                    "username": "user", "email": "test@example.com", "password": "mypassword"}}
            dockerconfig.write(json.dumps(dockerconfig_contents))
            dockerconfig.flush()
            secret_path = temp_dir

    if is_source_build:
        media_type = 'application/vnd.docker.distribution.manifest.v2+json'
    else:
        media_type = 'application/vnd.oci.image.manifest.v1+json'
    ref_name = "app/org.gnome.eog/x86_64/master"

    if not is_source_build:
        # Add a mock OCI image to 'flatpak_create_oci' results; this forces the tag_and_push
        # plugin to push with skopeo
        wf_data.plugins_results[PLUGIN_FLATPAK_CREATE_OCI] = {}

        # No need to create image archives, just need to mock its metadata
        for current_platform in platforms:
            if unsupported_image_type:
                image_type = 'unsupported_type'
            else:
                image_type = IMAGE_TYPE_OCI
            metadata = deepcopy(IMAGE_METADATA_OCI)
            metadata['ref_name'] = ref_name.replace('x86_64', current_platform)
            metadata['type'] = image_type
            workflow.data.plugins_results[PLUGIN_FLATPAK_CREATE_OCI][current_platform] = metadata

    # Mock the call to skopeo

    def check_run_skopeo(args):
        if fail_push:
            raise subprocess.CalledProcessError(returncode=1, cmd=args, output="Failed")
        assert args[0] == 'skopeo'
        if use_secret:
            assert '--authfile=' + os.path.join(secret_path, '.dockercfg') in args
        assert '--dest-tls-verify=false' in args
        if is_source_build:
            assert args[-2] == 'docker-archive://' + IMAGE_METADATA_DOCKER_ARCHIVE['path']
            output_image = 'docker://{}/{}:{}'.format(LOCALHOST_REGISTRY, sources_koji_repo,
                                                      sources_tagname)
            assert args[-1] == output_image
        else:
            current_platform = args[-1].split('-')[-1]
            assert args[-2] == ('oci:' + IMAGE_METADATA_OCI['path'] + ':' +
                                ref_name.replace('x86_64', current_platform))
            assert args[-1].startswith('docker://' + LOCALHOST_REGISTRY + f'/{TEST_IMAGE_NAME}')
            assert '--format=v2s2' in args
        return ''

    (flexmock(retries)
     .should_receive("run_cmd")
     .replace_with(check_run_skopeo))

    # Mock out the response from the registry once the OCI image is uploaded

    manifest_latest_url = "https://{}/v2/{}/manifests/latest".format(LOCALHOST_REGISTRY, TEST_IMAGE)
    manifest_url = "https://{}/v2/{}/manifests/{}".format(
        LOCALHOST_REGISTRY, TEST_IMAGE, DIGEST_OCI)
    manifest_source_tag_url = "https://{}/v2/{}/manifests/{}".format(
        LOCALHOST_REGISTRY, sources_koji_repo, sources_tagname)
    manifest_source_digest_url = "https://{}/v2/{}/manifests/{}".format(
        LOCALHOST_REGISTRY, sources_koji_repo, DIGEST_OCI)

    manifest_response = requests.Response()
    (flexmock(manifest_response,
              raise_for_status=lambda: None,
              json={},
              headers={
                'Content-Type': media_type,
                'Docker-Content-Digest': DIGEST_OCI
              }))

    manifest_unacceptable_response = requests.Response()
    (flexmock(manifest_unacceptable_response,
              status_code=404,
              json={
                  "errors": [{"code": "MANIFEST_UNKNOWN"}]
              }))

    def custom_get(method, url, headers, **kwargs):
        if url.startswith(manifest_latest_url) or url == manifest_source_tag_url:
            if headers['Accept'] == media_type:
                if is_source_build and not v2s2:
                    return manifest_unacceptable_response
                else:
                    return manifest_response
            else:
                return manifest_unacceptable_response

        if url == manifest_url or url == manifest_source_digest_url:
            return manifest_response

    mock_get_retry_session()

    (flexmock(requests.Session)
        .should_receive('request')
        .replace_with(custom_get))

    rcm = {'version': 1,
           'registries': [{'url': LOCALHOST_REGISTRY,
                           'insecure': True,
                           'auth': {'cfg_path': secret_path}}]}
    workflow.conf.conf = rcm
    add_koji_map_in_workflow(workflow, hub_url='', root_url='')

    runner = PostBuildPluginsRunner(
        workflow,
        [{
            'name': TagAndPushPlugin.key,
            'args': {
                'koji_target': sources_koji_target
            },
        }]
    )

    if fail_push or unsupported_image_type or (is_source_build and not v2s2):
        with pytest.raises(PluginFailedException):
            runner.run()

        if not fail_push and is_source_build and not v2s2:
            assert "Unable to fetch v2 schema 2 digest for" in caplog.text

        if unsupported_image_type and not fail_push:
            assert ('Attempt to push unsupported image type unsupported_type with skopeo' in
                    caplog.text)
    else:
        runner.run()

        assert workflow.conf.registry
        repos_annotations = get_repositories_annotations(wf_data.tag_conf)
        assert wf_data.annotations['repositories'] == repos_annotations


def test_skip_plugin(workflow, caplog):
    rcm = {'version': 1,
           'registries': [{'url': LOCALHOST_REGISTRY,
                           'insecure': True,
                           'auth': {}}]}
    workflow.conf.conf = rcm
    runner = PostBuildPluginsRunner(
        workflow,
        [{
            'name': TagAndPushPlugin.key,
            'args': {},
        }]
    )
    results = runner.run()[TagAndPushPlugin.key]
    assert 'not a flatpak or source build, skipping plugin' in caplog.text
    assert 'pushed_images' in results
    assert 'repositories' in results
    assert not results['pushed_images']
    repositories = results['repositories']
    assert 'primary' in repositories
    assert 'unique' in repositories
    assert 'floating' in repositories
    assert not repositories['primary']
    assert not repositories['unique']
    assert not repositories['floating']
