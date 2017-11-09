"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals
import pytest
import json
import hashlib
import re
import responses
from tempfile import mkdtemp
import os
import requests
from six import binary_type, text_type

from tests.constants import SOURCE, INPUT_IMAGE, MOCK, DOCKER0_REGISTRY

from atomic_reactor.core import DockerTasker
from atomic_reactor.build import BuildResult
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from atomic_reactor.inner import DockerBuildWorkflow, TagConf
from atomic_reactor.util import ImageName, registry_hostname, ManifestDigest
from atomic_reactor.plugins.post_group_manifests import GroupManifestsPlugin

if MOCK:
    from tests.docker_mock import mock_docker


def to_bytes(value):
    if isinstance(value, binary_type):
        return value
    else:
        return value.encode('utf-8')


def to_text(value):
    if isinstance(value, text_type):
        return value
    else:
        return text_type(value, 'utf-8')


def make_digest(blob):
    # Abbreviate the hexdigest for readability of debugging output if things fail
    return 'sha256:' + hashlib.sha256(to_bytes(blob)).hexdigest()[0:10]


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
                return (requests.codes.NOT_FOUND, {}, {'error': 'NOT_FOUND'})

        try:
            blob = repo['manifests'][ref]
        except KeyError:
            return (requests.codes.NOT_FOUND, {}, {'error': 'NOT_FOUND'})

        decoded = json.loads(to_text(blob))
        content_type = decoded['mediaType']

        accepts = re.split('\s*,\s*', req.headers['Accept'])
        assert content_type in accepts

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


def mock_registries(registries, config, schema_version='v2', foreign_layers=False):
    """
    Creates MockRegistries objects and fills them in based on config, which specifies
    which registries should be prefilled (as if by workers) with platform-specific
    manifests, and with what tags.
    """
    reg_map = {}
    for reg in registries:
        reg_map[reg] = MockRegistry(reg)

    worker_builds = {}

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

        worker_builds[platform] = {
            'digests': digests
        }

    return reg_map, {
        'worker-builds': worker_builds
    }


class Y(object):
    pass


class X(object):
    image_id = INPUT_IMAGE
    source = Y()
    source.dockerfile_path = None
    source.path = None
    base_image = ImageName(repo="qwe", tag="asd")


def mock_environment(tmpdir, primary_images=None,
                     annotations={}):
    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    base_image_id = '123456parent-id'
    setattr(workflow, '_base_image_inspect', {'Id': base_image_id})
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'image_id', '123456imageid')
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='22'))
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder, 'built_image_info', {'ParentId': base_image_id})
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)
    setattr(workflow, 'tag_conf', TagConf())
    if primary_images:
        workflow.tag_conf.add_primary_images(primary_images)
        workflow.tag_conf.add_unique_image(primary_images[0])

    workflow.build_result = BuildResult(image_id='123456', annotations=annotations)

    return tasker, workflow


REGISTRY_V1 = 'registry_v1.example.com'
REGISTRY_V2 = 'registry_v2.example.com'
OTHER_V2 = 'registry.example.com:5001'


@pytest.mark.parametrize('schema_version', ('v2', 'oci'))
@pytest.mark.parametrize(('test_name', 'group', 'foreign_layers',
                          'registries', 'workers', 'expected_exception'), [
    # Basic manifest grouping, v1 registry should be ignored
    ("group",
     True, False, [REGISTRY_V2, OTHER_V2, REGISTRY_V1],
     {
         'ppc64le': {
             REGISTRY_V2: ['namespace/httpd:worker-build-ppc64le-latest'],
             OTHER_V2: ['namespace/httpd:worker-build-ppc64le-latest'],
         },
         'x86_64': {
             REGISTRY_V2: ['namespace/httpd:worker-build-x86_64-latest'],
             OTHER_V2: ['namespace/httpd:worker-build-x86_64-latest'],
         }
     },
     None),
    # Have to copy the referenced manifests and link blobs from one repository to another
    ("group_link_manifests",
     True, False, [REGISTRY_V2],
     {
         'ppc64le': {
             REGISTRY_V2: ['worker-build:worker-build-ppc64le-latest'],
         },
         'x86_64': {
             REGISTRY_V2: ['worker-build:worker-build-x86_64-latest'],
         }
     },
     None),
    # Have to copy the referenced manifests and link blobs from one repository to another;
    # some layers of the image are foreign and thus not found to copy
    ("group_link_manifests_foreign",
     True, True, [REGISTRY_V2],
     {
         'ppc64le': {
             REGISTRY_V2: ['worker-build:worker-build-ppc64le-latest'],
         },
         'x86_64': {
             REGISTRY_V2: ['worker-build:worker-build-x86_64-latest'],
         }
     },
     None),
    # Some architectures aren't present for a registry, should error out
    ("group_missing_arches",
     True, False, [REGISTRY_V2],
     {
         'ppc64le': {
             REGISTRY_V2: ['namespace/httpd:worker-build-ppc64le-latest'],
         },
         'x86_64': {
         }
     },
     "Missing platforms for registry"),
    # No workers at all, should error out
    ("group_no_workers",
     True, False, [REGISTRY_V2],
     {
     },
     "No worker builds found"),
    # group=False, should tag x86_64 manifest with configured tags
    ("tag",
     False, False, [REGISTRY_V2, OTHER_V2, REGISTRY_V1],
     {
         'ppc64le': {
             REGISTRY_V2: ['namespace/httpd:worker-build-ppc64le-latest'],
             OTHER_V2: ['namespace/httpd:worker-build-ppc64le-latest'],
         },
         'x86_64': {
             REGISTRY_V2: ['namespace/httpd:worker-build-x86_64-latest'],
             OTHER_V2: ['namespace/httpd:worker-build-x86_64-latest'],
         }
     },
     None),
    # Have to copy the manifest and link blobs from one repository to another
    ("tag_link_manifests",
     False, False, [REGISTRY_V2],
     {
         'ppc64le': {
             REGISTRY_V2: ['worker-build:worker-build-ppc64le-latest'],
         },
         'x86_64': {
             REGISTRY_V2: ['worker-build:worker-build-x86_64-latest'],
         }
     },
     None),
    # No x86_64 found, should error out
    ("tag_no_x86_64",
     False, False, [REGISTRY_V2],
     {
         'ppc64le': {
             REGISTRY_V2: ['namespace/httpd:worker-build-ppc64le-latest'],
         },
     },
     "failed to find an x86_64 platform")
])
@responses.activate  # noqa
def test_group_manifests(tmpdir, test_name,
                         schema_version, group, foreign_layers, registries, workers,
                         expected_exception):
    if MOCK:
        mock_docker()

    test_images = ['namespace/httpd:2.4',
                   'namespace/httpd:latest']

    goarch = {
        'ppc64le': 'powerpc',
        'x86_64': 'amd64',
    }

    all_registry_conf = {
        REGISTRY_V1: {'version': 'v1', 'insecure': True},
        REGISTRY_V2: {'version': 'v2', 'insecure': True},
        OTHER_V2: {'version': 'v2', 'insecure': False},
    }

    temp_dir = mkdtemp(dir=str(tmpdir))
    with open(os.path.join(temp_dir, ".dockercfg"), "w+") as dockerconfig:
        dockerconfig_contents = {
            REGISTRY_V2: {
                "username": "user", "password": DOCKER0_REGISTRY
            }
        }
        dockerconfig.write(json.dumps(dockerconfig_contents))
        dockerconfig.flush()
        all_registry_conf[REGISTRY_V2]['secret'] = temp_dir

    registry_conf = {
        k: v for k, v in all_registry_conf.items() if k in registries
    }

    plugins_conf = [{
        'name': GroupManifestsPlugin.key,
        'args': {
            'registries': registry_conf,
            'group': group,
            'goarch': goarch,
        },
    }]

    mocked_registries, annotations = mock_registries(registry_conf, workers,
                                                     schema_version=schema_version,
                                                     foreign_layers=foreign_layers)
    tasker, workflow = mock_environment(tmpdir, primary_images=test_images,
                                        annotations=annotations)

    runner = PostBuildPluginsRunner(tasker, workflow, plugins_conf)
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

            for platform in workers:
                build = annotations['worker-builds'][platform]['digests'][0]
                source_builds[platform] = build
                source_registry = mocked_registries[build['registry']]
                source_manifests[platform] = source_registry.get_manifest(build['repository'],
                                                                          build['digest'])

            for registry, conf in registry_conf.items():
                if conf['version'] == 'v1':
                    continue

                target_registry = mocked_registries[registry]
                for image in test_images:
                    name, tag = image.split(':')

                    if tag not in target_registry.get_repo(name)['tags']:
                        continue

                    manifest_list = json.loads(to_text(target_registry.get_manifest(name, tag)))
                    assert manifest_list['mediaType'] == list_type
                    assert manifest_list['schemaVersion'] == 2

                    manifests = manifest_list['manifests']
                    assert all(d['mediaType'] == manifest_type for d in manifests)
                    assert all(d['platform']['os'] == 'linux' for d in manifests)

                    for platform in annotations['worker-builds']:
                        descs = [d for d in manifests
                                 if d['platform']['architecture'] == goarch[platform]]
                        assert len(descs) == 1
                        assert descs[0]['digest'] == source_builds[platform]['digest']

                        verify_manifest_in_repository(target_registry, name,
                                                      source_manifests[platform], platform)

        else:
            source_build = annotations['worker-builds']['x86_64']['digests'][0]
            source_registry = mocked_registries[source_build['registry']]
            source_manifest = source_registry.get_manifest(source_build['repository'],
                                                           source_build['digest'])

            for registry, conf in registry_conf.items():
                if conf['version'] == 'v1':
                    continue

                target_registry = mocked_registries[registry]
                for image in test_images:
                    name, tag = image.split(':')
                    if tag not in target_registry.get_repo(name)['tags']:
                        continue
                    verify_manifest_in_repository(target_registry, name,
                                                  source_manifest, 'x86_64',
                                                  tag)

        # Check that plugin returns ManifestDigest object
        plugin_result = results[GroupManifestsPlugin.key]
        assert isinstance(plugin_result, dict)

        for _, digest in plugin_result.items():
            assert isinstance(digest, ManifestDigest)

        if group:
            # Check that plugin returns correct list of repos
            actual_repos = sorted(plugin_result.keys())
            expected_repos = sorted(set([x.get_repo() for x in workflow.tag_conf.primary_images]))
            assert expected_repos == actual_repos

    else:
        with pytest.raises(PluginFailedException) as ex:
            runner.run()
        assert expected_exception in str(ex)
