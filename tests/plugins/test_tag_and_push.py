"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from datetime import datetime
import time
import platform
import random
import pytest
import koji as koji
import osbs
import atomic_reactor.plugins.post_tag_and_push
from atomic_reactor.constants import IMAGE_TYPE_OCI, IMAGE_TYPE_OCI_TAR
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.post_tag_and_push import ExceedsImageSizeError, TagAndPushPlugin
from atomic_reactor.plugins.pre_fetch_sources import PLUGIN_FETCH_SOURCES_KEY
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor.util import ManifestDigest, get_exported_image_metadata
from atomic_reactor.utils import retries
from tests.constants import (LOCALHOST_REGISTRY, TEST_IMAGE, TEST_IMAGE_NAME, INPUT_IMAGE, MOCK,
                             DOCKER0_REGISTRY, MOCK_SOURCE)
from tests.stubs import StubInsideBuilder
from tests.util import add_koji_map_in_workflow

import json
import os.path
from tempfile import mkdtemp
import requests
import subprocess
import tarfile
from base64 import b64encode

if MOCK:
    import docker
    from flexmock import flexmock
    from tests.docker_mock import mock_docker
    from tests.retry_mock import mock_get_retry_session

DIGEST_V1 = 'sha256:7de72140ec27a911d3f88d60335f08d6530a4af136f7beab47797a196e840afd'
DIGEST_V2 = 'sha256:85a7e3fb684787b86e64808c5b91d926afda9d6b35a0642a72d7a746452e71c1'
DIGEST_OCI = 'sha256:bb57e66a2dabcd59a721639b67bafb6d8aa35fbe0939d39a51b087b4504718e0'

DIGEST_LOG = 'sha256:hey-this-should-not-be-used'
PUSH_LOGS_1_10 = [
    {"status": "The push refers to a repository [localhost:5000/busybox]"},
    {"status": "Preparing", "progressDetail": {}, "id": "5f70bf18a086"},
    {"status": "Preparing", "progressDetail": {}, "id": "9508eff2c687"},
    {"status": "Pushing", "progressDetail": {"current": 721920, "total": 1113436}, "progress": "[================================>                  ] 721.9 kB/1.113 MB", "id": "9508eff2c687"},  # noqa
    {"status": "Pushing", "progressDetail": {"current": 1024}, "progress": "1.024 kB", "id": "5f70bf18a086"},  # noqa
    {"status": "Pushing", "progressDetail": {"current": 820224, "total": 1113436}, "progress": "[====================================>              ] 820.2 kB/1.113 MB", "id": "9508eff2c687"},  # noqa
    {"status": "Pushed", "progressDetail": {}, "id": "5f70bf18a086"},
    {"status": "Pushed", "progressDetail": {}, "id": "5f70bf18a086"},
    {"status": "Pushing", "progressDetail": {"current": 1300992, "total": 1113436}, "progress": "[==================================================>] 1.301 MB", "id": "9508eff2c687"},  # noqa
    {"status": "Pushing", "progressDetail": {"current": 1310720, "total": 1113436}, "progress": "[==================================================>] 1.311 MB", "id": "9508eff2c687"},  # noqa
    {"status": "Pushed", "progressDetail": {}, "id": "9508eff2c687"},
    {"status": "Pushed", "progressDetail": {}, "id": "9508eff2c687"},
    {"status": "latest: digest: + DIGEST_LOG.encode('utf-8') + size: 1920"},
    {"progressDetail": {}, "aux": {"Tag": "latest", "Digest": " + DIGEST_LOG.encode('utf-8') + ", "Size": 1920}}]  # noqa

PUSH_LOGS_1_10_NOT_IN_STATUS = list(PUSH_LOGS_1_10)
del PUSH_LOGS_1_10_NOT_IN_STATUS[-2]

PUSH_LOGS_1_9 = [
    {"status": "The push refers to a repository [172.17.42.1:5000/ns/test-image2] (len: 1)"},
    {"status": "Buffering to Disk", "progressDetail": {}, "id": "83bca0dcfd1b"},
    {"status": "Pushing", "progressDetail": {"current": 1, "total": 32}, "progress": "[=>                                                 ]      1 B/32 B", "id": "83bca0dcfd1b"},  # noqa
    {"status": "Pushing", "progressDetail": {"current": 66813953, "total": 66944370}, "progress": "[=================================================> ] 66.81 MB/66.94 MB", "id": "ded7cd95e059"},  # noqa
    {"status": "Pushing", "progressDetail": {"current": 66944370, "total": 66944370}, "progress": "[==================================================>] 66.94 MB/66.94 MB", "id": "ded7cd95e059"},  # noqa
    {"status": "Image successfully pushed", "progressDetail": {}, "id": "ded7cd95e059"},
    {"status": "Image already exists", "progressDetail": {}, "id": "48ecf305d2cf"},
    {"status": "Digest: + DIGEST_LOG.encode('utf-8') + "}]

PUSH_LOGS_1_X = [  # don't remember which version does this
    {"status": "The push refers to a repository [172.17.42.1:5000/ns/test-image2]"},
    {"status": "13cde7f2a483: Pushed "},
    {"status": "7.1-23: digest: + DIGEST_LOG.encode('utf-8') + size: 1539"}]

PUSH_ERROR_LOGS = [
    {"status": "The push refers to a repository [xyz/abc] (len: 1)"},
    {"errorDetail": {"message": "error message detail"}, "error": "error message"},
]


@pytest.mark.parametrize("use_secret", [
    True,
    False,
])
@pytest.mark.parametrize(("image_name", "logs", "should_raise", "has_config", "missing_v2"), [
    (TEST_IMAGE_NAME, PUSH_LOGS_1_X, False, False, False),
    (TEST_IMAGE_NAME, PUSH_LOGS_1_9, False, False, False),
    (TEST_IMAGE_NAME, PUSH_LOGS_1_10, False, True, False),
    (TEST_IMAGE_NAME, PUSH_LOGS_1_10_NOT_IN_STATUS, False, False, False),
    (TEST_IMAGE_NAME, PUSH_LOGS_1_X, False, False, True),
    (TEST_IMAGE_NAME, PUSH_LOGS_1_9, False, False, True),
    (TEST_IMAGE_NAME, PUSH_LOGS_1_10, False, False, True),
    (TEST_IMAGE_NAME, PUSH_LOGS_1_10_NOT_IN_STATUS, False, False, True),
    (DOCKER0_REGISTRY + '/' + TEST_IMAGE_NAME, PUSH_LOGS_1_X, True, False, False),
    (DOCKER0_REGISTRY + '/' + TEST_IMAGE_NAME, PUSH_LOGS_1_9, True, False, False),
    (DOCKER0_REGISTRY + '/' + TEST_IMAGE_NAME, PUSH_LOGS_1_10, True, True, False),
    (DOCKER0_REGISTRY + '/' + TEST_IMAGE_NAME, PUSH_LOGS_1_10_NOT_IN_STATUS, True, True, False),
    (TEST_IMAGE_NAME, PUSH_ERROR_LOGS, True, False, False),
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
        tmpdir, monkeypatch, caplog, user_params,
        image_name, logs, should_raise, has_config, missing_v2,
        use_secret, file_name, dockerconfig_contents):

    if MOCK:
        mock_docker()
        flexmock(docker.APIClient, push=lambda iid, **kwargs: iter(logs),
                 login=lambda username, registry, dockercfg_path: {'Status': 'Login Succeeded'})

    tasker = DockerTasker(retry_times=0)
    workflow = DockerBuildWorkflow(source=MOCK_SOURCE)
    workflow.user_params['image_tag'] = TEST_IMAGE
    workflow.tag_conf.add_primary_image(image_name)
    workflow.builder = StubInsideBuilder()
    workflow.builder.image_id = INPUT_IMAGE

    secret_path = None
    if use_secret:
        temp_dir = mkdtemp()
        with open(os.path.join(temp_dir, file_name), "w+") as dockerconfig:
            dockerconfig.write(json.dumps(dockerconfig_contents))
            dockerconfig.flush()
            secret_path = temp_dir

    CONFIG_DIGEST = 'sha256:2c782e3a93d34d89ea4cf54052768be117caed54803263dd1f3798ce42aac14e'
    media_type = 'application/vnd.docker.distribution.manifest.v2+json'

    manifest_json = {
        'config': {
            'digest': CONFIG_DIGEST,
            'mediaType': 'application/octet-stream',
            'size': 4132
        },
        'layers': [
            {
                'digest': 'sha256:16dc1f96e3a1bb628be2e00518fec2bb97bd5933859de592a00e2eb7774b6ecf',
                'mediaType': 'application/vnd.docker.image.rootfs.diff.tar.gzip',
                'size': 71907148
            },
            {
                'digest': 'sha256:cebc0565e1f096016765f55fde87a6f60fdb1208c0b5017e35a856ff578f5ccb',
                'mediaType': 'application/vnd.docker.image.rootfs.diff.tar.gzip',
                'size': 3945724
            }
        ],
        'mediaType': media_type,
        'schemaVersion': 2
    }

    config_json = {
        'config': {
            'Size': 12509448,
            'architecture': 'amd64',
            'author': 'Red Hat, Inc.',
            'config': {
                'Cmd': ['/bin/rsyslog.sh'],
                'Entrypoint': None,
                'Image': 'c3fb36aafd5692d2a45115d32bb120edb6edf6c0c3c783ed6592a8dab969fb88',
                'Labels': {
                    'Architecture': 'x86_64',
                    'Authoritative_Registry': 'registry.access.redhat.com',
                    'BZComponent': 'rsyslog-docker',
                    'Name': 'rhel7/rsyslog',
                    'Release': '28.vrutkovs.31',
                    'Vendor': 'Red Hat, Inc.',
                    'Version': '7.2',
                },
            },
            'created': '2016-10-07T10:20:05.38595Z',
            'docker_version': '1.9.1',
            'id': '1ca220fbc2aed7c141b236c8729fe59db5771b32bf2da74e4a663407f32ec2a2',
            'os': 'linux',
            'parent': '47eed7a8ea3f131e9281ae09fcbfb0041872fd8b74a048f1c739302c8148505d'
        },
        'container_config': {
            'foo': 'bar',
            'spam': 'maps'
        },
        'id': '1ca220fbc2aed7c141b236c8729fe59db5771b32bf2da74e4a663407f32ec2a2',
        'parent_id': 'c3fb36aafd5692d2a45115d32bb120edb6edf6c0c3c783ed6592a8dab969fb88'
    }

    # To test out the lack of a config, we really should be testing what happens
    # when we only return a v1 response and not a v2 response at all; what are
    # doing now is simply testing that if we return a None instead of json for the
    # config blob, that None is stored rather than json
    if not has_config:
        config_json = None

    manifest_latest_url = "https://{}/v2/{}/manifests/latest".format(LOCALHOST_REGISTRY, TEST_IMAGE)
    manifest_url = "https://{}/v2/{}/manifests/{}".format(LOCALHOST_REGISTRY, TEST_IMAGE, DIGEST_V2)
    config_blob_url = "https://{}/v2/{}/blobs/{}".format(
        LOCALHOST_REGISTRY, TEST_IMAGE, CONFIG_DIGEST)

    # We return our v2 manifest in the mocked v1 response as a placeholder - only the
    # digest matters anyways
    manifest_response_v1 = requests.Response()
    (flexmock(manifest_response_v1,
              status_code=200,
              json=manifest_json,
              headers={
                'Content-Type': 'application/vnd.docker.distribution.manifest.v1+json',
                'Docker-Content-Digest': DIGEST_V1
              }))

    manifest_response_v2 = requests.Response()
    (flexmock(manifest_response_v2,
              status_code=200,
              json=manifest_json,
              headers={
                'Content-Type': 'application/vnd.docker.distribution.manifest.v2+json',
                'Docker-Content-Digest': DIGEST_V2
              }))
    manifest_response_v2_list = requests.Response()
    (flexmock(manifest_response_v2_list,
              raise_for_status=lambda: None,
              json=manifest_json,
              headers={
                'Content-Type': 'application/vnd.docker.distribution.manifest.list.v2+json',
              }))

    config_blob_response = requests.Response()
    (flexmock(config_blob_response, status_code=200, json=config_json))

    manifest_unknown_response = requests.Response()
    (flexmock(manifest_unknown_response,
              status_code=404,
              json={
                  "errors": [{"code": "MANIFEST_UNKNOWN"}]
              }))

    def custom_get(method, url, headers, **kwargs):
        if url == manifest_latest_url:
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

        if url == config_blob_url:
            return config_blob_response

    mock_get_retry_session()
    (flexmock(requests.Session)
        .should_receive('request')
        .replace_with(custom_get))
    (flexmock(time)
        .should_receive('sleep')
        .and_return(None))

    workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
    workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
        ReactorConfig({'version': 1, 'registries': [{
            'url': LOCALHOST_REGISTRY,
            'insecure': True,
            'auth': {'cfg_path': secret_path}}],
                        'group_manifests': missing_v2})
    add_koji_map_in_workflow(workflow, hub_url='', root_url='')

    runner = PostBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': TagAndPushPlugin.key,
            'args': {
                'registries': {
                    LOCALHOST_REGISTRY: {
                        'insecure': True,
                        'secret': secret_path
                    }
                }
            },
        }]
    )

    if should_raise:
        with pytest.raises(PluginFailedException):
            runner.run()
    else:
        output = runner.run()
        image = output[TagAndPushPlugin.key][0]
        tasker.remove_image(image)
        assert len(workflow.push_conf.docker_registries) > 0

        if MOCK:
            # we only test this when mocking docker because we don't expect
            # running actual docker against v2 registry
            if missing_v2:
                expected_digest = ManifestDigest(v1=DIGEST_V1, v2=None, oci=None)
                assert "Retrying push because V2 schema 2" in caplog.text
            else:
                expected_digest = ManifestDigest(v1=DIGEST_V1, v2=DIGEST_V2, oci=None)
                assert workflow.push_conf.docker_registries[0].digests[image_name].v2 == \
                    expected_digest.v2

            assert workflow.push_conf.docker_registries[0].digests[image_name].v1 == \
                expected_digest.v1
            assert workflow.push_conf.docker_registries[0].digests[image_name].oci == \
                expected_digest.oci

            if has_config:
                assert isinstance(workflow.push_conf.docker_registries[0].config, dict)
            else:
                assert workflow.push_conf.docker_registries[0].config is None


@pytest.mark.parametrize(("source_oci_image_path", "v2s2"), [
    (True, True),
    (True, False),
    (False, True),
    (False, False)
])
@pytest.mark.parametrize("use_secret", [
    True,
    False,
])
@pytest.mark.parametrize("fail_push", [
    False,
    True,
])
def test_tag_and_push_plugin_oci(tmpdir, monkeypatch, user_params,
                                 source_oci_image_path, v2s2, use_secret,
                                 fail_push, caplog):
    # For now, we don't want to require having a skopeo and an OCI-supporting
    # registry in the test environment
    if MOCK:
        mock_docker()
    else:
        return

    sources_dir_path = '/oci_source_image_path'
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

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(source=MOCK_SOURCE)
    workflow.user_params['image_tag'] = TEST_IMAGE
    workflow.builder = StubInsideBuilder()
    workflow.builder.image_id = INPUT_IMAGE
    if source_oci_image_path:
        workflow.build_result._oci_image_path = sources_dir_path
        workflow.prebuild_results[PLUGIN_FETCH_SOURCES_KEY] =\
            {'sources_for_koji_build_id': sources_koji_id}

    class MockedClientSession(object):
        def __init__(self, hub, opts=None):
            pass

        def getBuild(self, build_info):
            if source_oci_image_path:
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

    CONFIG_DIGEST = 'sha256:b79482f7dcab2a326c1e8c7025a4336d900e99f50db8b35a659fda67b5ebb3c2'
    if source_oci_image_path:
        MEDIA_TYPE = 'application/vnd.docker.distribution.manifest.v2+json'
    else:
        MEDIA_TYPE = 'application/vnd.oci.image.manifest.v1+json'
    REF_NAME = "app/org.gnome.eog/x86_64/master"

    manifest_json = {
        "schemaVersion": 2,
        "mediaType": MEDIA_TYPE,
        "config": {
            "mediaType": MEDIA_TYPE,
            "digest": CONFIG_DIGEST,
            "size": 314
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": "sha256:fd2b341d2ff3751ecdee8d8daacaa650d8a1703360c85d4cfc452d6ec32e147f",
                "size": 1863477
            }
        ],
        "annotations": {
            "org.flatpak.commit-metadata.xa.ref": "YXBwL29yZy5nbm9tZS5lb2cveDg2XzY0L21hc3RlcgAAcw==",  # noqa
            "org.flatpak.body": "Name: org.gnome.eog\nArch: x86_64\nBranch: master\nBuilt with: Flatpak 0.9.7\n",  # noqa
            "org.flatpak.commit-metadata.xa.metadata": "W0FwcGxpY2F0aW9uXQpuYW1lPW9yZy5nbm9tZS5lb2cKcnVudGltZT1vcmcuZmVkb3JhcHJvamVjdC5QbGF0Zm9ybS94ODZfNjQvMjYKc2RrPW9yZy5mZWRvcmFwcm9qZWN0LlBsYXRmb3JtL3g4Nl82NC8yNgpjb21tYW5kPWVvZwoKW0NvbnRleHRdCnNoYXJlZD1pcGM7CnNvY2tldHM9eDExO3dheWxhbmQ7c2Vzc2lvbi1idXM7CmZpbGVzeXN0ZW1zPXhkZy1ydW4vZGNvbmY7aG9zdDt+Ly5jb25maWcvZGNvbmY6cm87CgpbU2Vzc2lvbiBCdXMgUG9saWN5XQpjYS5kZXNydC5kY29uZj10YWxrCgpbRW52aXJvbm1lbnRdCkRDT05GX1VTRVJfQ09ORklHX0RJUj0uY29uZmlnL2Rjb25mCgAAcw==",  # noqa
            "org.flatpak.download-size": "1863477",
            "org.flatpak.commit-metadata.xa.download-size": "AAAAAAAdF/IAdA==",
            "org.flatpak.commit-metadata.xa.installed-size": "AAAAAABDdgAAdA==",
            "org.flatpak.subject": "Export org.gnome.eog",
            "org.flatpak.installed-size": "4421120",
            "org.flatpak.commit": "d7b8789350660724b20643ebb615df466566b6d04682fa32800d3f10116eec54",  # noqa
            "org.flatpak.metadata": "[Application]\nname=org.gnome.eog\nruntime=org.fedoraproject.Platform/x86_64/26\nsdk=org.fedoraproject.Platform/x86_64/26\ncommand=eog\n\n[Context]\nshared=ipc;\nsockets=x11;wayland;session-bus;\nfilesystems=xdg-run/dconf;host;~/.config/dconf:ro;\n\n[Session Bus Policy]\nca.desrt.dconf=talk\n\n[Environment]\nDCONF_USER_CONFIG_DIR=.config/dconf\n",  # noqa
            "org.opencontainers.image.ref.name": REF_NAME,
            "org.flatpak.timestamp": "1499376525"
        }
    }

    config_json = {
        "created": "2017-07-06T21:28:45Z",
        "architecture": "arm64",
        "os": "linux",
        "config": {
            "Memory": 0,
            "MemorySwap": 0,
            "CpuShares": 0
        },
        "rootfs": {
            "type": "layers",
            "diff_ids": [
                "sha256:4c5160fea65110aa1eb8ca022e2693bb868367c2502855887f21c77247199339"
            ]
        }
    }

    # Add a mock OCI image to exported_image_sequence; this forces the tag_and_push
    # plugin to push with skopeo rather than with 'docker push'

    # Since we are always mocking the push for now, we can get away with a stub image
    oci_dir = os.path.join(str(tmpdir), 'oci-image')
    os.mkdir(oci_dir)
    with open(os.path.join(oci_dir, "index.json"), "w") as f:
        f.write('"Not a real index.json"')
    with open(os.path.join(oci_dir, "oci-layout"), "w") as f:
        f.write('{"imageLayoutVersion": "1.0.0"}')
    os.mkdir(os.path.join(oci_dir, 'blobs'))

    metadata = get_exported_image_metadata(oci_dir, IMAGE_TYPE_OCI)
    metadata['ref_name'] = REF_NAME
    workflow.exported_image_sequence.append(metadata)

    oci_tarpath = os.path.join(str(tmpdir), 'oci-image.tar')
    with open(oci_tarpath, "wb") as f:
        with tarfile.TarFile(mode="w", fileobj=f) as tf:
            for f in os.listdir(oci_dir):
                tf.add(os.path.join(oci_dir, f), f)

    metadata = get_exported_image_metadata(oci_tarpath, IMAGE_TYPE_OCI_TAR)
    metadata['ref_name'] = REF_NAME
    workflow.exported_image_sequence.append(metadata)

    # Mock the call to skopeo

    def check_run_skopeo(args):
        if fail_push:
            raise subprocess.CalledProcessError(returncode=1, cmd=args, output="Failed")
        assert args[0] == 'skopeo'
        if use_secret:
            assert '--authfile=' + os.path.join(secret_path, '.dockercfg') in args
        assert '--dest-tls-verify=false' in args
        if source_oci_image_path:
            assert args[-2] == 'oci:' + sources_dir_path
            output_image = 'docker://{}/{}:{}'.format(LOCALHOST_REGISTRY, sources_koji_repo,
                                                      sources_tagname)
            assert args[-1] == output_image
        else:
            assert args[-2] == 'oci:' + oci_dir + ':' + REF_NAME
            assert args[-1] == 'docker://' + LOCALHOST_REGISTRY + '/' + TEST_IMAGE_NAME
        return ''

    (flexmock(retries)
     .should_receive("run_cmd")
     .once()
     .replace_with(check_run_skopeo))

    # Mock out the response from the registry once the OCI image is uploaded

    manifest_latest_url = "https://{}/v2/{}/manifests/latest".format(LOCALHOST_REGISTRY, TEST_IMAGE)
    manifest_url = "https://{}/v2/{}/manifests/{}".format(
        LOCALHOST_REGISTRY, TEST_IMAGE, DIGEST_OCI)
    manifest_source_tag_url = "https://{}/v2/{}/manifests/{}".format(
        LOCALHOST_REGISTRY, sources_koji_repo, sources_tagname)
    manifest_source_digest_url = "https://{}/v2/{}/manifests/{}".format(
        LOCALHOST_REGISTRY, sources_koji_repo, DIGEST_OCI)
    config_blob_url = "https://{}/v2/{}/blobs/{}".format(
        LOCALHOST_REGISTRY, TEST_IMAGE, CONFIG_DIGEST)
    source_config_blob_url = "https://{}/v2/{}/blobs/{}".format(
        LOCALHOST_REGISTRY, sources_koji_repo, CONFIG_DIGEST)

    manifest_response = requests.Response()
    (flexmock(manifest_response,
              raise_for_status=lambda: None,
              json=manifest_json,
              headers={
                'Content-Type': MEDIA_TYPE,
                'Docker-Content-Digest': DIGEST_OCI
              }))

    manifest_unacceptable_response = requests.Response()
    (flexmock(manifest_unacceptable_response,
              status_code=404,
              json={
                  "errors": [{"code": "MANIFEST_UNKNOWN"}]
              }))

    config_blob_response = requests.Response()
    (flexmock(config_blob_response, raise_for_status=lambda: None, json=config_json))

    def custom_get(method, url, headers, **kwargs):
        if url == manifest_latest_url or url == manifest_source_tag_url:
            if headers['Accept'] == MEDIA_TYPE:
                if source_oci_image_path and not v2s2:
                    return manifest_unacceptable_response
                else:
                    return manifest_response
            else:
                return manifest_unacceptable_response

        if url == manifest_url or url == manifest_source_digest_url:
            return manifest_response

        if url == config_blob_url or url == source_config_blob_url:
            return config_blob_response

    mock_get_retry_session()

    (flexmock(requests.Session)
        .should_receive('request')
        .replace_with(custom_get))

    workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
    workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
        ReactorConfig({'version': 1,
                       'registries': [{'url': LOCALHOST_REGISTRY,
                                       'insecure': True,
                                       'auth': {'cfg_path': secret_path}}]})
    add_koji_map_in_workflow(workflow, hub_url='', root_url='')

    runner = PostBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': TagAndPushPlugin.key,
            'args': {
                'registries': {
                    LOCALHOST_REGISTRY: {
                        'insecure': True,
                        'secret': secret_path
                    }
                },
                'koji_target': sources_koji_target
            },
        }]
    )

    if fail_push or (source_oci_image_path and not v2s2):
        with pytest.raises(PluginFailedException):
            runner.run()

        if not fail_push and source_oci_image_path and not v2s2:
            assert "Unable to fetch v2 schema 2 digest for" in caplog.text
    else:
        output = runner.run()

        image = output[TagAndPushPlugin.key][0]
        tasker.remove_image(image)
        assert len(workflow.push_conf.docker_registries) > 0

        push_conf_digests = workflow.push_conf.docker_registries[0].digests

        if source_oci_image_path:
            source_image_name = '{}:{}'.format(sources_koji_repo, sources_tagname)
            assert push_conf_digests[source_image_name].v1 is None
            assert push_conf_digests[source_image_name].v2 == DIGEST_OCI
            assert push_conf_digests[source_image_name].oci is None
        else:
            assert push_conf_digests[TEST_IMAGE_NAME].v1 is None
            assert push_conf_digests[TEST_IMAGE_NAME].v2 is None
            assert push_conf_digests[TEST_IMAGE_NAME].oci == DIGEST_OCI

        assert workflow.push_conf.docker_registries[0].config is config_json


@pytest.mark.parametrize('image_size_limit', [
    None,                       # omit the config from reactor config map
    {'binary_image': 0},       # checking image size should be skipped
    {'binary_image': 2500}     # maximum image size that should make the code raise an error
])
def test_exceed_binary_image_size(image_size_limit, workflow):
    config = {
        'version': 1,
        'registries': [
            {'url': LOCALHOST_REGISTRY}
        ],
    }
    if image_size_limit is not None:
        config['image_size_limit'] = image_size_limit

    # workflow = DockerBuildWorkflow(source=MOCK_SOURCE)
    workflow.plugin_workspace[ReactorConfigPlugin.key] = {
        WORKSPACE_CONF_KEY: ReactorConfig(config)
    }
    workflow.builder = StubInsideBuilder()
    workflow.builder.image_id = INPUT_IMAGE
    # fake layer sizes of the test image
    workflow.layer_sizes = [
        {'diff_id': '12345', 'size': 1000},
        {'diff_id': '23456', 'size': 2000},
        {'diff_id': '34567', 'size': 3000},
    ]

    mock_docker()

    plugin = TagAndPushPlugin(DockerTasker(), workflow)

    if image_size_limit is None or image_size_limit['binary_image'] == 0:
        # The plugin should skip the check on image size

        (flexmock(atomic_reactor.plugins.post_tag_and_push)
         .should_receive('get_manifest_digests')
         .and_return(ManifestDigest({
             'v2': 'application/vnd.docker.distribution.manifest.list.v2+json',
         })))

        (flexmock(atomic_reactor.plugins.post_tag_and_push)
         .should_receive('get_config_from_registry'))

        assert workflow.image == plugin.run()[0].repo
    else:
        with pytest.raises(ExceedsImageSizeError):
            plugin.run()
