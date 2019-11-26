"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals, absolute_import
import pytest
import json
import hashlib
import re
import responses
from tempfile import mkdtemp
import os
from six import binary_type, text_type

from tests.constants import MOCK, DOCKER0_REGISTRY
from tests.stubs import StubInsideBuilder

from atomic_reactor.core import DockerTasker
from atomic_reactor.build import BuildResult
from atomic_reactor.plugin import ExitPluginsRunner
from atomic_reactor.inner import TagConf
from atomic_reactor.util import ImageName, registry_hostname, ManifestDigest
from atomic_reactor.plugins.exit_push_floating_tags import PushFloatingTagsPlugin
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin, WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor.constants import PLUGIN_GROUP_MANIFESTS_KEY, PLUGIN_BUILD_ORCHESTRATE_KEY


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
        self._add_pattern(responses.PUT, r'/v2/(.*)/manifests/([^/]+)',
                          self._put_manifest)

    def get_repo(self, name):
        return self.repos.setdefault(name, {
            'blobs': {},
            'manifests': {},
            'tags': {},
        })

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

    def _put_manifest(self, req, name, ref):
        try:
            json.loads(to_text(req.body))
        except ValueError:
            return (400, {}, {'error': 'BAD_MANIFEST'})

        self.add_manifest(name, ref, req.body)
        return (200, {}, '')


def mock_registries(registries, config, primary_images=None, manifest_results=None,
                    schema_version='v2'):
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
            manifest = {'schemaVersion': 2}

            if schema_version == 'v2':
                manifest['mediaType'] = 'application/vnd.docker.distribution.manifest.v2+json'
            elif schema_version == 'oci':
                manifest['mediaType'] = 'application/vnd.oci.image.manifest.v1+json'

            for t in tags:
                name, tag = t.split(':')
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

    if primary_images and manifest_results:
        for _, registry in reg_map.items():
            for image in primary_images:
                name, tag = image.split(':')
                repo = registry.get_repo(name)
                manifest_digest = manifest_results["manifest_digest"]
                repo["manifests"][manifest_digest.default] = manifest_results["manifest"]
                repo["tags"][tag] = manifest_digest.default

    return reg_map, {
        'worker-builds': worker_builds,
        'repositories': {'primary': primary_images or [], 'floating': []}
    }


def mock_environment(tmpdir, workflow, primary_images=None, floating_images=None,
                     manifest_results=None, annotations=None):
    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    base_image_id = '123456parent-id'
    setattr(workflow, '_base_image_inspect', {'Id': base_image_id})
    setattr(workflow, 'builder', StubInsideBuilder())
    setattr(workflow.builder, 'image_id', '123456imageid')
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='22'))
    setattr(workflow.builder, 'source', StubInsideBuilder())
    setattr(workflow.builder, 'built_image_info', {'ParentId': base_image_id})
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)
    setattr(workflow, 'tag_conf', TagConf())
    if primary_images:
        for image in primary_images:
            if '-' in ImageName.parse(image).tag:
                workflow.tag_conf.add_primary_image(image)
        workflow.tag_conf.add_unique_image(primary_images[0])

    if floating_images:
        workflow.tag_conf.add_floating_images(floating_images)

    workflow.build_result = BuildResult(image_id='123456', annotations=annotations or {})
    workflow.postbuild_results = {}
    if manifest_results:
        workflow.postbuild_results[PLUGIN_GROUP_MANIFESTS_KEY] = manifest_results

    return tasker, workflow


REGISTRY_V2 = 'registry_v2.example.com'
OTHER_V2 = 'registry.example.com:5001'


GROUPED_V2_RESULTS = {
    "manifest_digest": ManifestDigest(v2_list="sha256:11c3ecdbfa"),
    "media_type": "application/vnd.docker.distribution.manifest.list.v2+json",
    "manifest": json.dumps({
        "manifests": [
            {
                "digest": "sha256:9dc3bbcd6c",
                "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                "platform": {
                    "architecture": "amd64",
                    "os": "linux"
                },
                "size": 306
            },
            {
                "digest": "sha256:cd619643ae",
                "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                "platform": {
                    "architecture": "powerpc",
                    "os": "linux"
                },
                "size": 306
            }
        ],
        "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
        "schemaVersion": 2
    }, indent=4, sort_keys=True, separators=(',', ': '))
}
GROUPED_OCI_RESULTS = {
    "manifest_digest": ManifestDigest(oci_index="sha256:cf4d07b24d"),
    "media_type": "application/vnd.oci.image.index.v1+json",
    "manifest": json.dumps({
        "manifests": [
            {
                "digest": "sha256:62cef32411",
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "platform": {
                    "architecture": "amd64",
                    "os": "linux"
                },
                "size": 279
            },
            {
                "digest": "sha256:c1c380151b",
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "platform": {
                    "architecture": "powerpc",
                    "os": "linux"
                },
                "size": 279
            }
        ],
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "schemaVersion": 2
    }, indent=4, sort_keys=True, separators=(',', ': '))
}
NOGROUP_V2_RESULTS = {
    "manifest_digest": ManifestDigest(v2="sha256:cd619643ae"),
    "media_type": "application/vnd.docker.distribution.manifest.v2+json",
    "manifest": json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {
            "mediaType": "application/vnd.docker.container.image.v1+json",
            "digest": "sha256:0efa9f4e8f"
        },
        "layers": [
            {
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                "digest": "sha256:aca5f3af1d"
            }
        ]
    }),
}
NOGROUP_OCI_RESULTS = {
    "manifest_digest": ManifestDigest(oci="sha256:c1c380151b"),
    "media_type": "application/vnd.oci.image.manifest.v1+json",
    "manifest": json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": "sha256:0efa9f4e8f"
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
                "digest": "sha256:aca5f3af1d"
                }
        ]
    }),
}


@pytest.mark.parametrize(('test_name',
                          'registries', 'manifest_results', 'schema_version',
                          'floating_tags',
                          'workers', 'expected_exception'), [
    ("simple_grouped_v2",
     [REGISTRY_V2, OTHER_V2], GROUPED_V2_RESULTS, 'v2',
     ['namespace/httpd:2.4-1'],
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
    ("simple_grouped_oci",
     [REGISTRY_V2, OTHER_V2], GROUPED_OCI_RESULTS, 'oci',
     ['namespace/httpd:2.4-1'],
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
    ("multi_v2",
     [REGISTRY_V2, OTHER_V2], GROUPED_V2_RESULTS, 'v2',
     ['namespace/httpd:2.4-1', 'namespace/httpd:latest'],
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
    ("simple_ungrouped_v2",
     [REGISTRY_V2], NOGROUP_V2_RESULTS, 'v2',
     ['namespace/httpd:2.4-1'],
     {
         'ppc64le': {
             REGISTRY_V2: ['namespace/httpd:worker-build-ppc64le-latest'],
         }
     },
     None),
    ("simple_ungrouped_oci",
     [REGISTRY_V2], NOGROUP_OCI_RESULTS, 'oci',
     ['namespace/httpd:2.4-1'],
     {
         'ppc64le': {
             REGISTRY_V2: ['namespace/httpd:worker-build-ppc64le-latest'],
         }
     },
     None),
    ("multi_ungrouped_v2",
     [REGISTRY_V2], NOGROUP_V2_RESULTS, 'v2',
     ['namespace/httpd:2.4-1', 'namespace/httpd:latest'],
     {
         'ppc64le': {
             REGISTRY_V2: ['namespace/httpd:worker-build-ppc64le-latest'],
         }
     },
     None),
    ("multi_ungrouped_oci",
     [REGISTRY_V2], NOGROUP_OCI_RESULTS, 'oci',
     ['namespace/httpd:2.4-1', 'namespace/httpd:latest'],
     {
         'ppc64le': {
             REGISTRY_V2: ['namespace/httpd:worker-build-ppc64le-latest'],
         }
     },
     None),
    ("No tags",
     [REGISTRY_V2, OTHER_V2], GROUPED_V2_RESULTS, 'v2',
     None,
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
     'No floating images to tag, skipping push_floating_tags'),
    ("called_from_worker",
     [REGISTRY_V2, OTHER_V2], GROUPED_V2_RESULTS, 'v2',
     ['namespace/httpd:2.4-1', 'namespace/httpd:latest'],
     {},
     'push_floating_tags cannot be used by a worker builder'),
    ("No_results",
     [REGISTRY_V2, OTHER_V2], None, 'oci',
     ['namespace/httpd:2.4-1', 'namespace/httpd:latest'],
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
     'No manifest digest available, skipping push_floating_tags'),
])
@responses.activate  # noqa
def test_floating_tags_push(tmpdir, workflow, test_name, registries, manifest_results,
                            schema_version, floating_tags, workers, expected_exception,
                            caplog):
    if MOCK:
        mock_docker()

    primary_images = ['namespace/httpd:2.4', 'namespace/httpd:primary']

    goarch = {
        'ppc64le': 'powerpc',
        'x86_64': 'amd64',
    }

    all_registry_conf = {
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
        'name': PushFloatingTagsPlugin.key,
    }]

    mocked_registries, annotations = mock_registries(registry_conf, workers,
                                                     primary_images=primary_images,
                                                     manifest_results=manifest_results,
                                                     schema_version=schema_version)
    tasker, workflow = mock_environment(tmpdir, workflow, primary_images=primary_images,
                                        floating_images=floating_tags,
                                        manifest_results=manifest_results,
                                        annotations=annotations)

    if workers:
        workflow.buildstep_plugins_conf = [{'name': PLUGIN_BUILD_ORCHESTRATE_KEY}]

    registries_list = []

    for docker_uri in registry_conf:
        reg_ver = registry_conf[docker_uri]['version']
        reg_secret = None
        if 'secret' in registry_conf[docker_uri]:
            reg_secret = registry_conf[docker_uri]['secret']

        new_reg = {}
        if reg_secret:
            new_reg['auth'] = {'cfg_path': reg_secret}
        else:
            new_reg['auth'] = {'cfg_path': str(temp_dir)}
        new_reg['url'] = 'https://' + docker_uri + '/' + reg_ver

        registries_list.append(new_reg)

    platform_descriptors_list = []
    for platform in goarch:
        new_plat = {
            'platform': platform,
            'architecture': goarch[platform],
        }
        platform_descriptors_list.append(new_plat)

    workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
    workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
        ReactorConfig({'version': 1,
                       'registries': registries_list,
                       'platform_descriptors': platform_descriptors_list})

    runner = ExitPluginsRunner(tasker, workflow, plugins_conf)
    results = runner.run()
    plugin_result = results[PushFloatingTagsPlugin.key]

    if expected_exception is None:
        primary_name, primary_tag = primary_images[0].split(':')
        for registry in registry_conf:
            target_registry = mocked_registries[registry]
            primary_manifest_list = target_registry.get_manifest(primary_name, primary_tag)

            for image in floating_tags:
                name, tag = image.split(':')

                assert tag in target_registry.get_repo(name)['tags']
                assert target_registry.get_manifest(name, tag) == primary_manifest_list

        # Check that plugin returns ManifestDigest object
        assert isinstance(plugin_result, dict)
        # Check that plugin returns correct list of repos
        actual_repos = sorted(plugin_result.keys())
        expected_repos = sorted(x.get_repo() for x in workflow.tag_conf.images)
        assert expected_repos == actual_repos
    else:
        assert not plugin_result
        assert expected_exception in caplog.text
