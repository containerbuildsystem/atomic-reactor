"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals
import pytest
import json
import responses
from copy import deepcopy
from tempfile import mkdtemp
import os
from flexmock import flexmock
import subprocess
from subprocess import CalledProcessError
from requests.exceptions import ConnectionError

from tests.constants import SOURCE, INPUT_IMAGE, MOCK, DOCKER0_REGISTRY

from atomic_reactor.core import DockerTasker
from atomic_reactor.build import BuildResult
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from atomic_reactor.inner import DockerBuildWorkflow, TagConf
from atomic_reactor.util import ImageName
from atomic_reactor.plugins.post_group_manifests import GroupManifestsPlugin

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


X86_DIGESTS = [
    {
        'digest': 'sha256:worker-build-x86_64-digest',
        'tag': 'worker-build-x86_64-latest',
        'registry': DOCKER0_REGISTRY,
        'repository': 'worker-build-x86_64-repository',
    },
]
X86_ANNOTATIONS = {
    'build': {
        'build-name': 'worker-build-x86_64',
        'cluster-url': 'https://worker_x86_64.com/',
        'namespace': 'worker_x86_64_namespace'
    },
    'digests': X86_DIGESTS,
    'plugins-metadata': {},
}
PPC_DIGESTS = [
    {
        'digest': 'sha256:worker-build-ppc64le-digest',
        'tag': 'worker-build-ppc64le-latest',
        'registry': 'worker-build-ppc64le-registry',
        'repository': 'worker-build-ppc64le-repository',
    },
]
PPC_ANNOTATIONS = {
    'build': {
        'build-name': 'worker-build-ppc64le',
        'cluster-url': 'https://worker_ppc64le.com/',
        'namespace': 'worker_ppc64le_namespace'
    },
    'digests': PPC_DIGESTS,
    'plugins-metadata': {}
}

BUILD_ANNOTATIONS = {
        'worker-builds': {
        },
        'repositories': {
            'unique': [
                'worker-build-ppc64le-unique',
                'worker-build-x86_64-unique',
            ],
            'primary': [
                'worker-build-ppc64le-primary',
                'worker-build-x86_64-primary',
            ],
        },
    }
V1_REGISTRY = "10.10.0.1:5000"


def mock_environment(tmpdir, primary_images=None,
                     worker_annotations={}):
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

    annotations = deepcopy(BUILD_ANNOTATIONS)
    if not worker_annotations:
        worker_annotations = {'ppc64le': PPC_ANNOTATIONS}
    for worker in worker_annotations:
        annotations['worker-builds'][worker] = deepcopy(worker_annotations[worker])

    workflow.build_result = BuildResult(image_id='123456', annotations=annotations)

    return tasker, workflow


def mock_url_responses(docker_registry, test_images, worker_digests, version='2', respond=True):
    def verify_put_body(req):
        assert req.body == body.encode('utf-8')
        return (status, req.headers, '')

    responses.reset()
    for worker_digest in worker_digests:
        digest = worker_digest[0]['digest']
        repo = worker_digest[0]['repository']
        for registry in docker_registry:
            if not registry.startswith('http://') and not registry.startswith('https://'):
                registry = 'https://' + registry
            url = '{0}/v2/{1}/manifests/{2}'.format(registry, repo, digest)
            body = json.dumps({'tag': 'testtag', 'schemaVersion': version}, indent=2)
            responses.add(responses.GET, url, body=body)
            if respond:
                status = 200
            else:
                status = 400
                body = json.dumps({'error': 'INVALID MANIFEST'})
            for image_tag in test_images:
                url = '{0}/v2/{1}/manifests/{2}'.format(registry, repo, image_tag.split(':')[1])
                responses.add_callback(responses.PUT, url, callback=verify_put_body)


class TestGroupManifests(object):
    @pytest.mark.parametrize(('goarch', 'worker_annotations', 'valid'), [
        ({}, {}, False),
        ({'ppc64le': 'powerpc', 'x86_64': 'amd64'},
         {'ppc64le': PPC_ANNOTATIONS, 'x86_64': X86_ANNOTATIONS}, True),
        ({'ppc64le': 'powerpc', 'x86_64': 'amd64'},
         {'ppc64le': PPC_ANNOTATIONS, 'x86_64': X86_ANNOTATIONS}, False),
    ])
    @responses.activate  # noqa
    def test_group_manifests_true(self, tmpdir, goarch, worker_annotations, valid):
        if MOCK:
            mock_docker()

        test_images = ['registry.example.com/namespace/httpd:2.4',
                       'registry.example.com/namespace/httpd:latest']
        expected_results = set()

        registries = {
            DOCKER0_REGISTRY: {'version': 'v2', 'insecure': True},
            V1_REGISTRY: {'version': 'v2', 'insecure': True},
        }

        plugins_conf = [{
            'name': GroupManifestsPlugin.key,
            'args': {
                'registries': registries,
                'group': True,
                'goarch': goarch,
            },
        }]
        tasker, workflow = mock_environment(tmpdir, primary_images=test_images,
                                            worker_annotations=worker_annotations)

        def request_callback(request):
            media_type = request.headers['Accept']
            if media_type.endswith('list.v2+json'):
                digest = 'v2_list-digest:{0}'.format(request.url)
            else:
                raise ValueError('Unexpected media type {}'.format(media_type))

            media_type_prefix = media_type.split('+')[0]
            headers = {
                'Content-Type': '{}+jsonish'.format(media_type_prefix),
                'Docker-Content-Digest': digest
            }
            return (200, headers, '')

        for registry in registries:
            if valid:
                repo_and_tag = workflow.tag_conf.images[0].to_str(registry=False).split(':')
                url = 'http://{0}/v2/{1}/manifests/{2}'.format(registry, repo_and_tag[0],
                                                               repo_and_tag[1])
                expected_results.add('v2_list-digest:{0}'.format(url))
            for image in workflow.tag_conf.images:
                repo_and_tag = image.to_str(registry=False).split(':')
                path = '/v2/{0}/manifests/{1}'.format(repo_and_tag[0], repo_and_tag[1])
                https_url = 'https://' + registry + path
                responses.add(responses.GET, https_url, body=ConnectionError())
                url = 'http://' + registry + path
                if valid:
                    responses.add_callback(responses.GET, url, callback=request_callback)

        (flexmock(subprocess)
         .should_receive("check_output"))

        runner = PostBuildPluginsRunner(tasker, workflow, plugins_conf)
        if valid:
            result = runner.run()
            test_results = set()
            for digest in result['group_manifests']:
                test_results.add(digest.v2_list)
            assert test_results == expected_results
        else:
            with pytest.raises(PluginFailedException):
                runner.run()

    @responses.activate  # noqa
    def test_group_manifests_manifest_tool_fail(self, tmpdir):
        if MOCK:
            mock_docker()

        goarch = {'ppc64le': 'powerpc', 'x86_64': 'amd64'}
        worker_annotations = {'ppc64le': PPC_ANNOTATIONS, 'x86_64': X86_ANNOTATIONS}

        test_images = ['registry.example.com/namespace/httpd:2.4',
                       'registry.example.com/namespace/httpd:latest']

        registries = {
            DOCKER0_REGISTRY: {'version': 'v2', 'insecure': True},
            V1_REGISTRY: {'version': 'v2', 'insecure': True},
        }

        plugins_conf = [{
            'name': GroupManifestsPlugin.key,
            'args': {
                'registries': registries,
                'group': True,
                'goarch': goarch,
            },
        }]
        tasker, workflow = mock_environment(tmpdir, primary_images=test_images,
                                            worker_annotations=worker_annotations)

        (flexmock(subprocess)
         .should_receive("check_output")
         .and_raise(CalledProcessError))

        runner = PostBuildPluginsRunner(tasker, workflow, plugins_conf)
        with pytest.raises(PluginFailedException):
            runner.run()

    @pytest.mark.parametrize('use_secret', [True, False])
    @pytest.mark.parametrize('version', ['1', '2'])
    @pytest.mark.parametrize(('goarch', 'worker_annotations', 'valid', 'respond'), [
        ({}, {}, False, True),
        ({}, {'x86_64': X86_ANNOTATIONS}, False, True),
        ({'x86_64': 'amd64'}, {}, False, True),
        ({'x86_64': 'amd64'}, {'x86_64': X86_ANNOTATIONS}, True, True),
        ({'ppc64le': 'powerpc', 'x86_64': 'amd64'},
         {'ppc64le': PPC_ANNOTATIONS, 'x86_64': X86_ANNOTATIONS}, True, True),
        ({'ppc64le': 'powerpc', 'x86_64': 'amd64'},
         {'ppc64le': PPC_ANNOTATIONS, 'x86_64': X86_ANNOTATIONS}, True, False),
    ])
    @responses.activate  # noqa
    def test_group_manifests_false(self, tmpdir, use_secret, goarch,
                                   worker_annotations, version, valid, respond):
        if MOCK:
            mock_docker()

        if version == '1':
            valid = False

        test_images = ['registry.example.com/namespace/httpd:2.4',
                       'registry.example.com/namespace/httpd:latest']

        registries = {
            DOCKER0_REGISTRY: {'version': 'v2'},
            V1_REGISTRY: {'version': 'v1'},
        }
        if use_secret:
            temp_dir = mkdtemp(dir=str(tmpdir))
            with open(os.path.join(temp_dir, ".dockercfg"), "w+") as dockerconfig:
                dockerconfig_contents = {
                    DOCKER0_REGISTRY: {
                        "username": "user", "password": DOCKER0_REGISTRY
                    }
                }
                dockerconfig.write(json.dumps(dockerconfig_contents))
                dockerconfig.flush()
                registries[DOCKER0_REGISTRY]['secret'] = temp_dir

        plugins_conf = [{
            'name': GroupManifestsPlugin.key,
            'args': {
                'registries': registries,
                'group': False,
                'goarch': goarch,
            },
        }]
        tasker, workflow = mock_environment(tmpdir, primary_images=test_images,
                                            worker_annotations=worker_annotations)
        mock_url_responses([DOCKER0_REGISTRY], test_images, [X86_DIGESTS], version, respond)

        runner = PostBuildPluginsRunner(tasker, workflow, plugins_conf)
        if valid and respond:
            result = runner.run()
            assert result['group_manifests'] == []
            expected_digests = {}
            for image in workflow.tag_conf.primary_images:
                expected_digests[image.tag] = "sha256:worker-build-x86_64-digest"
            assert workflow.push_conf.docker_registries
            assert expected_digests == workflow.push_conf.docker_registries[0].digests
        else:
            with pytest.raises(PluginFailedException):
                runner.run()
