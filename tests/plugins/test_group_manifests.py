"""
Copyright (c) 2017, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from functools import partial

import pytest
import json
import re
import responses
from tempfile import mkdtemp
import os
import requests
from collections import OrderedDict

from flexmock import flexmock

from tests.constants import DOCKER0_REGISTRY
from tests.mock_env import MockEnv

from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.inner import TagConf
from atomic_reactor.util import (registry_hostname, ManifestDigest, get_floating_images,
                                 get_primary_images, sha256sum, RegistrySession, RegistryClient)
from atomic_reactor.utils.manifest import ManifestUtil
from atomic_reactor.plugins.group_manifests import GroupManifestsPlugin, BuiltImage
from osbs.utils import ImageName


def to_bytes(value):
    if isinstance(value, bytes):
        return value
    else:
        return value.encode('utf-8')


def to_text(value):
    if isinstance(value, str):
        return value
    else:
        return str(value, 'utf-8')


make_digest = partial(sha256sum, abbrev_len=10, prefix=True)


class MockRegistry(object):
    """
    This class mocks a subset of the v2 Docker Registry protocol
    """
    def __init__(self, registry):
        self.hostname = registry_hostname(registry)
        self.repos = {}
        self._add_pattern(responses.GET, r'/v2/(.*)/manifests/([^/]+)',
                          self._get_manifest)
        self._add_pattern(responses.HEAD, r'/v2/(.*)/manifests/([^/]+)',
                          self._get_manifest)
        self._add_pattern(responses.PUT, r'/v2/(.*)/manifests/([^/]+)',
                          self._put_manifest)
        self._add_pattern(responses.GET, r'/v2/(.*)/blobs/([^/]+)',
                          self._get_blob)
        self._add_pattern(responses.HEAD, r'/v2/(.*)/blobs/([^/]+)',
                          self._get_blob)
        self._add_pattern(responses.POST, r'/v2/(.*)/blobs/uploads/\?mount=([^&]+)&from=(.+)',
                          self._mount_blob)

    def get_repo(self, name):
        return self.repos.setdefault(name, {
            'blobs': {},
            'manifests': {},
            'tags': {},
        })

    def add_blob(self, name, blob):
        repo = self.get_repo(name)
        digest = make_digest(blob)
        repo['blobs'][digest] = blob
        return digest

    def get_blob(self, name, digest):
        return self.get_repo(name)['blobs'][digest]

    def add_manifest(self, name, ref, manifest):
        repo = self.get_repo(name)
        digest = make_digest(manifest)
        repo['manifests'][digest] = manifest
        if ref.startswith('sha256:'):
            assert ref == digest
        else:
            repo['tags'][ref] = digest
        return digest

    def get_manifest(self, name, ref):
        repo = self.get_repo(name)
        if not ref.startswith('sha256:'):
            ref = repo['tags'][ref]
        return repo['manifests'][ref]

    def _add_pattern(self, method, pattern, callback):
        pat = re.compile(r'^https://' + self.hostname + pattern + '$')

        def do_it(req):
            status, headers, body = callback(req, *(pat.match(req.url).groups()))
            if method == responses.HEAD:
                return status, headers, ''
            else:
                return status, headers, body

        responses.add_callback(method, pat, do_it, match_querystring=True)

    def _get_manifest(self, req, name, ref):
        repo = self.get_repo(name)
        if not ref.startswith('sha256:'):
            try:
                ref = repo['tags'][ref]
            except KeyError:
                return (requests.codes.NOT_FOUND, {}, b"{'error': 'NOT_FOUND'}")

        try:
            blob = repo['manifests'][ref]
        except KeyError:
            return (requests.codes.NOT_FOUND, {}, {'error': 'NOT_FOUND'})

        decoded = json.loads(to_text(blob))
        content_type = decoded['mediaType']

        headers = {
            'Docker-Content-Digest': ref,
            'Content-Type': content_type,
            'Content-Length': str(len(blob)),
        }
        return (200, headers, blob)

    def _put_manifest(self, req, name, ref):
        try:
            json.loads(to_text(req.body))
        except ValueError:
            return (400, {}, {'error': 'BAD_MANIFEST'})

        self.add_manifest(name, ref, req.body)
        return (200, {}, '')

    def _get_blob(self, req, name, digest):
        repo = self.get_repo(name)
        assert digest.startswith('sha256:')

        try:
            blob = repo['blobs'][digest]
        except KeyError:
            return (requests.codes.NOT_FOUND, {}, {'error': 'NOT_FOUND'})

        headers = {
            'Docker-Content-Digest': digest,
            'Content-Type': 'application/json',
            'Content-Length': str(len(blob)),
        }
        return (200, headers, blob)

    def _mount_blob(self, req, target_name, digest, source_name):
        source_repo = self.get_repo(source_name)
        target_repo = self.get_repo(target_name)

        try:
            target_repo['blobs'][digest] = source_repo['blobs'][digest]
            headers = {
                'Location': '/v2/{}/blobs/{}'.format(target_name, digest),
                'Docker-Content-Digest': digest,
            }
            return (201, headers, '')
        except KeyError:
            headers = {
                'Location': '/v2/{}/blobs/uploads/some-uuid'.format(target_name),
                'Docker-Upload-UUID': 'some-uuid',
            }
            return (202, headers, '')


def mock_registries(registries, config, schema_version='v2', foreign_layers=False,
                    manifest_list_tag=None):
    """
    Creates MockRegistries objects and fills them in based on config, which specifies
    which registries should be prefilled (as if by the per-platform build tasks) with
    platform-specific manifests, and with what tags.
    """
    reg_map = {}
    for reg in registries:
        reg_map[reg] = MockRegistry(reg)

    per_platform_digests = {}

    manifest_list = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
        "manifests": [
            {
                "platform": {
                    "os": "linux",
                    "architecture": "amd64"
                },
                "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                "digest": make_digest('v2digest-amd64'),
                # 'size': required by spec, skipped for test
            }
        ]
    }

    for platform, regs in config.items():
        digests = []

        for reg, tags in regs.items():
            registry = reg_map[reg]
            layer_digest = make_digest('layer-' + platform)
            config_digest = make_digest('config-' + platform)

            if schema_version == 'v2':
                manifest = {
                    'schemaVersion': 2,
                    'mediaType': 'application/vnd.docker.distribution.manifest.v2+json',
                    'config': {
                        'mediaType': 'application/vnd.docker.container.image.v1+json',
                        'digest':  config_digest,
                        # 'size': required by spec, skipped for test
                    },
                    'layers': [{
                        'mediaType': 'application/vnd.docker.image.rootfs.diff.tar.gzip',
                        'digest': layer_digest,
                        # 'size': required, skipped for test
                    }]
                }
                if foreign_layers:
                    manifest['layers'].append({
                        'mediaType': 'application/vnd.docker.image.rootfs.foreign.diff.tar.gzip',
                        'digest': make_digest('foreign-layer-' + platform),
                        'urls': ['https://example.com/example-layer']
                    })
            elif schema_version == 'oci':
                manifest = {
                    'schemaVersion': 2,
                    'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                    'config': {
                        'mediaType': 'application/vnd.oci.image.config.v1+json',
                        'digest': config_digest,
                        # 'size': required by spec, skipped for test
                    },
                    'layers': [{
                        'mediaType': 'application/vnd.oci.image.layer.v1.tar',
                        'digest': layer_digest,
                        # 'size': required, skipped for test
                    }]
                }
                if foreign_layers:
                    manifest['layers'].append({
                        'mediaType': 'application/vnd.oci.image.layer.nondistributable.v1.tar',
                        'digest': make_digest('foreign-layer-' + platform),
                        'urls': ['https://example.com/example-layer']
                    })

            for t in tags:
                name, tag = t.split(':')
                registry.add_blob(name, 'layer-' + platform)
                registry.add_blob(name, 'config-' + platform)
                manifest_bytes = to_bytes(json.dumps(manifest))
                digest = registry.add_manifest(name, tag, manifest_bytes)
                digests.append({
                    'registry': reg,
                    'repository': name,
                    'tag': tag,
                    'digest': digest,
                    'version': schema_version
                })
                digests.append({
                    'registry': reg,
                    'repository': name,
                    'tag': tag,
                    'digest': 'not-used',
                    'version': 'v1'
                })
            if manifest_list_tag:
                name, tag = manifest_list_tag.split(':')
                manifest_bytes = to_bytes(json.dumps(manifest_list))
                registry.add_manifest(name, tag, manifest_bytes)

        per_platform_digests[platform] = {
            'digests': digests
        }

    return reg_map, per_platform_digests


def mock_environment(workflow, unique_image, primary_images=None):
    wf_data = workflow.data
    setattr(wf_data, 'tag_conf', TagConf())
    if primary_images:
        for image in primary_images:
            if '-' in ImageName.parse(image).tag:
                wf_data.tag_conf.add_primary_image(image)

    wf_data.tag_conf.add_unique_image(unique_image)
    wf_data.tag_conf.add_floating_image('namespace/httpd:floating')


REGISTRY_V2 = 'registry_v2.example.com'


@pytest.mark.parametrize('schema_version', ('v2', 'oci'))
@pytest.mark.parametrize(('test_name', 'group', 'foreign_layers',
                          'per_platform_images', 'expected_exception'), [
    # Basic manifest grouping
    ("group",
     True, False,
     {
         # NOTE: all the per-platform images need to have the same base name, only the platform
         #  at the end should be different and must match the actual platform
         'ppc64le': ['namespace/httpd:2.4-ppc64le'],
         'x86_64': ['namespace/httpd:2.4-x86_64']
     },
     None),
    # Have to copy the referenced manifests and link blobs from one repository to another
    ("group_link_manifests",
     True, False,
     {
         'ppc64le': ['other-namespace/also-httpd:different-tag-ppc64le'],
         'x86_64': ['other-namespace/also-httpd:different-tag-x86_64']
     },
     None),
    # Have to copy the referenced manifests and link blobs from one repository to another;
    # some layers of the image are foreign and thus not found to copy
    ("group_link_manifests_foreign",
     True, True,
     {
         'ppc64le': ['other-namespace/also-httpd:different-tag-ppc64le'],
         'x86_64': ['other-namespace/also-httpd:different-tag-x86_64']
     },
     None),
    # Some architectures aren't present for a registry, should error out
    ("group_missing_arches",
     True, False,
     {
         'ppc64le': ['namespace/httpd:2.4-ppc64le'],
         'x86_64': []
     },
     # We query the registry for the manifest digests of built images, if an image is not there,
     #  we should get a 404
     "404 Client Error"),
    # group=False, should fail as we expect only one entry if not grouped
    ("tag",
     False, False,
     {
         'ppc64le': ['namespace/httpd:2.4-ppc64le'],
         'x86_64': ['namespace/httpd:2.4-x86_64']
     },
     "Without grouping only one built image is expected"),
    # Have to copy the manifest and link blobs from one repository to another
    ("tag_link_manifests",
     False, False,
     {
         'x86_64': ['other-namespace/also-httpd:different-tag-x86_64']
     },
     None),
    # No x86_64 found, but still have ppc64le
    ("tag_no_x86_64",
     False, False,
     {
         'ppc64le': ['namespace/httpd:2.4-ppc64le']
     },
     None),
])
@responses.activate  # noqa
def test_group_manifests(workflow, source_dir, schema_version, test_name, group, foreign_layers,
                         per_platform_images, expected_exception, user_params):
    test_images = ['namespace/httpd:2.4',
                   'namespace/httpd:latest']

    goarch = {
        'ppc64le': 'powerpc',
        'x86_64': 'amd64',
    }

    registry_conf = {REGISTRY_V2: {'version': 'v2', 'insecure': True}}

    temp_dir = mkdtemp(dir=str(source_dir))
    with open(os.path.join(temp_dir, ".dockercfg"), "w+") as dockerconfig:
        dockerconfig_contents = {
            REGISTRY_V2: {
                "username": "user", "password": DOCKER0_REGISTRY
            }
        }
        dockerconfig.write(json.dumps(dockerconfig_contents))
        dockerconfig.flush()
        registry_conf[REGISTRY_V2]['secret'] = temp_dir

    registry_images_conf = {
        platform: {REGISTRY_V2: images} for platform, images in per_platform_images.items()
    }

    mocked_registries, platform_digests = mock_registries(registry_conf, registry_images_conf,
                                                          schema_version=schema_version,
                                                          foreign_layers=foreign_layers)

    some_per_platform_image = next(
        image for images in per_platform_images.values() for image in images
    )
    # NOTE: this test assumes that all the images in per_platform_images follow the format of
    #   {noarch_image}-{platform}. If they don't, this test will fail with cryptic errors
    noarch_image, *_ = some_per_platform_image.rsplit("-", 1)
    mock_environment(workflow, unique_image=noarch_image, primary_images=test_images)

    registries_list = [
        {
            'url': f'https://{docker_uri}/{registry["version"]}',
            'auth': {'cfg_path': registry.get('secret', str(temp_dir))},
        }
        for docker_uri, registry in registry_conf.items()
    ]

    platform_descriptors_list = []
    for platform, arch in goarch.items():
        new_plat = {
            'platform': platform,
            'architecture': arch,
        }
        platform_descriptors_list.append(new_plat)

    runner = (
        MockEnv(workflow)
        .for_plugin(GroupManifestsPlugin.key)
        .set_check_platforms_result(list(per_platform_images.keys()))
        .set_reactor_config(
            {
                'version': 1,
                'group_manifests': group,
                'registries': registries_list,
                'platform_descriptors': platform_descriptors_list,
            }
        )
        .create_runner()
    )

    if expected_exception is None:
        results = runner.run()

        manifest_type, list_type = {
            'v2': (
                'application/vnd.docker.distribution.manifest.v2+json',
                'application/vnd.docker.distribution.manifest.list.v2+json',
            ),
            'oci': (
                'application/vnd.oci.image.manifest.v1+json',
                'application/vnd.oci.image.index.v1+json',
            ),
        }[schema_version]

        def verify_manifest_in_repository(registry, repo, manifest, platform, tag=None):
            config = 'config-' + platform
            assert registry.get_blob(repo, make_digest(config)) == config
            layer = 'layer-' + platform
            assert registry.get_blob(repo, make_digest(layer)) == layer
            assert registry.get_manifest(repo, make_digest(manifest)) == manifest
            if tag is not None:
                assert registry.get_manifest(repo, tag) == manifest

        if group:
            source_builds = {}
            source_manifests = {}

            for platform in per_platform_images:
                build = platform_digests[platform]['digests'][0]
                source_builds[platform] = build
                source_registry = mocked_registries[build['registry']]
                source_manifests[platform] = source_registry.get_manifest(build['repository'],
                                                                          build['digest'])

            for registry, conf in registry_conf.items():
                target_registry = mocked_registries[registry]
                for image in test_images:
                    name, tag = image.split(':')

                    if tag not in target_registry.get_repo(name)['tags']:
                        continue

                    raw_manifest_list = to_text(target_registry.get_manifest(name, tag))
                    manifest_list = json.loads(raw_manifest_list, object_pairs_hook=OrderedDict)

                    # Check if the manifest list is sorted
                    assert json.dumps(manifest_list, indent=4, sort_keys=True,
                                      separators=(',', ': ')) == raw_manifest_list
                    arch_list = [m['platform']['architecture'] for m in manifest_list['manifests']]
                    assert arch_list == sorted(arch_list)

                    assert manifest_list['mediaType'] == list_type
                    assert manifest_list['schemaVersion'] == 2

                    manifests = manifest_list['manifests']
                    assert all(d['mediaType'] == manifest_type for d in manifests)
                    assert all(d['platform']['os'] == 'linux' for d in manifests)

                    for platform in platform_digests:
                        descs = [d for d in manifests
                                 if d['platform']['architecture'] == goarch[platform]]
                        assert len(descs) == 1
                        assert descs[0]['digest'] == source_builds[platform]['digest']

                        verify_manifest_in_repository(target_registry, name,
                                                      source_manifests[platform], platform)

        else:
            platforms = list(platform_digests)
            assert len(platforms) == 1
            platform = platforms[0]

            source_build = platform_digests[platform]['digests'][0]
            source_registry = mocked_registries[source_build['registry']]
            source_manifest = source_registry.get_manifest(source_build['repository'],
                                                           source_build['digest'])

            for registry, conf in registry_conf.items():
                if conf['version'] == 'v1':
                    continue

                target_registry = mocked_registries[registry]
                for image in get_primary_images(workflow):
                    repo = image.to_str(registry=False, tag=False)
                    if image.tag not in target_registry.get_repo(repo)['tags']:
                        continue
                    verify_manifest_in_repository(target_registry, repo,
                                                  source_manifest, platform,
                                                  image.tag)
                for image in get_floating_images(workflow):
                    repo = image.to_str(registry=False, tag=False)
                    assert image.tag not in target_registry.get_repo(repo)['tags']

        # Check that plugin returns ManifestDigest object
        plugin_results = results[GroupManifestsPlugin.key]

        result_digest = plugin_results["manifest_digest"]
        assert isinstance(result_digest, ManifestDigest)

        result_digest = plugin_results["manifest_digest"]
        assert isinstance(result_digest, ManifestDigest)
        assert plugin_results["media_type"]
        assert plugin_results["manifest"]

    else:
        with pytest.raises(PluginFailedException) as ex:
            runner.run()
        assert expected_exception in str(ex.value)


UNIQUE_IMAGE = f'{REGISTRY_V2}/namespace/httpd:2.4'


@pytest.mark.parametrize("manifest_version", ["v2", "oci"])
@responses.activate
def test_get_built_images(workflow, manifest_version):
    MockEnv(workflow).set_check_platforms_result(["ppc64le", "x86_64"])
    workflow.data.tag_conf.add_unique_image(UNIQUE_IMAGE)

    _, platform_digests = mock_registries(
        [REGISTRY_V2],
        {
            "ppc64le": {REGISTRY_V2: ["namespace/httpd:2.4-ppc64le"]},
            "x86_64": {REGISTRY_V2: ["namespace/httpd:2.4-x86_64"]},
        },
        schema_version=manifest_version,
    )

    ppc_digest = platform_digests["ppc64le"]["digests"][0]["digest"]
    x86_digest = platform_digests["x86_64"]["digests"][0]["digest"]

    flexmock(ManifestUtil).should_receive("__init__")  # and do nothing, this test doesn't use it

    plugin = GroupManifestsPlugin(workflow)
    session = RegistrySession(REGISTRY_V2)

    assert plugin.get_built_images(session) == [
        BuiltImage(
            pullspec=ImageName.parse(f"{UNIQUE_IMAGE}-ppc64le"),
            platform="ppc64le",
            manifest_digest=ppc_digest,
            manifest_version=manifest_version,
        ),
        BuiltImage(
            pullspec=ImageName.parse(f"{UNIQUE_IMAGE}-x86_64"),
            platform="x86_64",
            manifest_digest=x86_digest,
            manifest_version=manifest_version,
        ),
    ]


@responses.activate
def test_get_built_images_multiple_manifest_types(workflow):
    MockEnv(workflow).set_check_platforms_result(["x86_64"])
    workflow.data.tag_conf.add_unique_image(UNIQUE_IMAGE)

    flexmock(ManifestUtil).should_receive("__init__")  # and do nothing, this test doesn't use it

    (
        flexmock(RegistryClient)
        .should_receive("get_manifest_digests")
        .with_args(ImageName.parse(f"{UNIQUE_IMAGE}-x86_64"), versions=("v2", "oci"))
        .and_return(ManifestDigest({"v2": make_digest("foo"), "oci": make_digest("bar")}))
    )

    plugin = GroupManifestsPlugin(workflow)
    session = RegistrySession(REGISTRY_V2)

    expect_error = (
        f"Expected to find a single manifest digest for {UNIQUE_IMAGE}-x86_64, "
        "but found multiple: {'v2': .*, 'oci': .*}"
    )

    with pytest.raises(RuntimeError, match=expect_error):
        plugin.get_built_images(session)
