"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import BuildCanceledException, PluginFailedException
from atomic_reactor.plugin import BuildStepPluginsRunner
from atomic_reactor.plugins import pre_reactor_config
from atomic_reactor.plugins.build_orchestrate_build import OrchestrateBuildPlugin
from atomic_reactor.plugins.pre_reactor_config import ReactorConfig
from atomic_reactor.util import ImageName, df_parser
from dockerfile_parse import DockerfileParser
from flexmock import flexmock
from multiprocessing.pool import AsyncResult
from osbs.api import OSBS
from osbs.conf import Configuration
from osbs.build.build_response import BuildResponse
from osbs.exceptions import OsbsException
from tests.constants import MOCK_SOURCE, TEST_IMAGE, INPUT_IMAGE, SOURCE
from tests.docker_mock import mock_docker
from textwrap import dedent

import json
import os
import pytest
import time


class MockSource(object):

    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = os.path.join(tmpdir, 'Dockerfile')
        self.path = tmpdir

    def get_dockerfile_path(self):
        return self.dockerfile_path, self.path


class MockInsideBuilder(object):

    def __init__(self):
        mock_docker()
        self.tasker = DockerTasker()
        self.base_image = ImageName(repo='fedora', tag='25')
        self.image_id = 'image_id'
        self.image = INPUT_IMAGE
        self.df_path = 'df_path'
        self.df_dir = 'df_dir'

        def simplegen(x, y):
            yield "some\u2018".encode('utf-8')
        flexmock(self.tasker, build_image_from_path=simplegen)

    def get_built_image_info(self):
        return {'Id': 'some'}

    def inspect_built_image(self):
        return None

    def ensure_not_built(self):
        pass


def mock_workflow(tmpdir):
    workflow = DockerBuildWorkflow(MOCK_SOURCE, TEST_IMAGE)
    builder = MockInsideBuilder()
    source = MockSource(tmpdir)
    setattr(builder, 'source', MockSource(tmpdir))
    setattr(workflow, 'source', MockSource(tmpdir))
    setattr(workflow, 'builder', builder)

    df_path = os.path.join(str(tmpdir), 'Dockerfile')
    with open(df_path, 'w') as f:
        f.write(dedent("""\
            FROM fedora:25
            LABEL com.redhat.component=python \
                  version=2.7 \
                  release=10
            """))
    df = df_parser(df_path)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    return workflow


def mock_reactor_config(tmpdir, clusters=None):
    if not clusters:
        clusters = {
            'x86_64': [
                {
                    'name': 'worker_x86_64',
                    'max_concurrent_builds': 3
                }
            ],
            'ppc64le': [
                {
                    'name': 'worker_ppc64le',
                    'max_concurrent_builds': 3
                }
            ]
        }
    conf = ReactorConfig({'version': 1, 'clusters': clusters})
    (flexmock(pre_reactor_config)
        .should_receive('get_config')
        .and_return(conf))

    with open(os.path.join(str(tmpdir), 'osbs.conf'), 'w') as f:
        for platform, plat_clusters in clusters.items():
            for cluster in plat_clusters:
                f.write(dedent("""\
                    [{name}]
                    openshift_url = https://{name}.com/
                    namespace = {name}_namespace
                    """.format(name=cluster['name'])))


def mock_osbs(current_builds=2, worker_builds=1, logs_return_bytes=False):
    (flexmock(OSBS)
        .should_receive('list_builds')
        .and_return(range(current_builds)))

    def mock_create_worker_build(**kwargs):
        return make_build_response('worker-build-{}'.format(kwargs['platform']),
                                   'Running')
    (flexmock(OSBS)
        .should_receive('create_worker_build')
        .replace_with(mock_create_worker_build))

    if logs_return_bytes:
        log_format_string = b'line \xe2\x80\x98 - %d'
    else:
        log_format_string = 'line \u2018 - %d'

    (flexmock(OSBS)
        .should_receive('get_build_logs')
        .and_yield(log_format_string % line for line in range(10)))

    def mock_wait_for_build_to_finish(build_name):
        return make_build_response(build_name, 'Complete')
    (flexmock(OSBS)
        .should_receive('wait_for_build_to_finish')
        .replace_with(mock_wait_for_build_to_finish))


def make_build_response(name, status, annotations=None, labels=None):
    build_response = {
        'metadata': {
            'name': name,
            'annotations': annotations or {},
            'labels': labels or {},
        },
        'status': {
            'phase': status
        }
    }

    return BuildResponse(build_response)


def make_worker_build_kwargs(**overrides):
    kwargs = {
        'git_uri': SOURCE['uri'],
        'git_ref': 'master',
        'git_branch': 'master',
        'user': 'bacon'
    }
    kwargs.update(overrides)
    return kwargs


@pytest.mark.parametrize('config_kwargs', [
    None,
    {},
    {'build_image': 'osbs-buildroot:latest'},
    {'build_image': 'osbs-buildroot:latest', 'sources_command': 'fedpkg source'},
])
@pytest.mark.parametrize('worker_build_image', [
    'fedora:latest',
    None
])
@pytest.mark.parametrize('logs_return_bytes', [
    True,
    False
])
def test_orchestrate_build(tmpdir, config_kwargs, worker_build_image, logs_return_bytes):
    workflow = mock_workflow(tmpdir)
    mock_osbs(logs_return_bytes=logs_return_bytes)
    mock_reactor_config(tmpdir)
    plugin_args = {
        'platforms': ['x86_64'],
        'build_kwargs': make_worker_build_kwargs(),
        'osbs_client_config': str(tmpdir),
    }
    if worker_build_image:
        plugin_args['worker_build_image'] = worker_build_image
    if config_kwargs is not None:
        plugin_args['config_kwargs'] = config_kwargs

    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': plugin_args
        }]
    )

    expected_kwargs = {
        'conf_section': 'worker_x86_64',
        'conf_file': tmpdir + '/osbs.conf',
    }
    if worker_build_image:
        expected_kwargs['build_image'] = worker_build_image
    # Update with config_kwargs last to ensure that, when set
    # always has precedence over worker_build_image param.
    if config_kwargs is not None:
        expected_kwargs.update(config_kwargs)

    (flexmock(Configuration).should_call('__init__').with_args(**expected_kwargs).once())

    build_result = runner.run()
    assert not build_result.is_failed()

    assert (build_result.annotations == {
        'worker-builds': {
            'x86_64': {
                'build': {
                    'build-name': 'worker-build-x86_64',
                    'cluster-url': 'https://worker_x86_64.com/',
                    'namespace': 'worker_x86_64_namespace'
                },
                'digests': [],
                'plugins-metadata': {}
            }
        }
    })

    assert (build_result.labels == {})


def test_orchestrate_build_annotations_and_labels(tmpdir):
    workflow = mock_workflow(tmpdir)
    mock_osbs()

    def mock_wait_for_build_to_finish(build_name):
        annotations = {
            'repositories': json.dumps({
                'unique': ['{}-unique'.format(build_name)],
                'primary': ['{}-primary'.format(build_name)],
            }),
            'digests': json.dumps([
                {
                    'digest': 'sha256:{}-digest'.format(build_name),
                    'tag': '{}-latest'.format(build_name),
                    'registry': '{}-registry'.format(build_name),
                    'repository': '{}-repository'.format(build_name),
                },
            ]),
        }
        labels = {'koji-build-id': 'koji-build-id'}
        return make_build_response(build_name, 'Complete', annotations, labels)
    (flexmock(OSBS)
        .should_receive('wait_for_build_to_finish')
        .replace_with(mock_wait_for_build_to_finish))

    mock_reactor_config(tmpdir)
    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                'platforms': ['x86_64', 'ppc64le'],
                'build_kwargs': make_worker_build_kwargs(),
                'osbs_client_config': str(tmpdir),
            }
        }]
    )
    build_result = runner.run()
    assert not build_result.is_failed()

    assert (build_result.annotations == {
        'worker-builds': {
            'x86_64': {
                'build': {
                    'build-name': 'worker-build-x86_64',
                    'cluster-url': 'https://worker_x86_64.com/',
                    'namespace': 'worker_x86_64_namespace'
                },
                'digests': [
                    {
                        'digest': 'sha256:worker-build-x86_64-digest',
                        'tag': 'worker-build-x86_64-latest',
                        'registry': 'worker-build-x86_64-registry',
                        'repository': 'worker-build-x86_64-repository',
                    },
                ],
                'plugins-metadata': {}
            },
            'ppc64le': {
                'build': {
                    'build-name': 'worker-build-ppc64le',
                    'cluster-url': 'https://worker_ppc64le.com/',
                    'namespace': 'worker_ppc64le_namespace'
                },
                'digests': [
                    {
                        'digest': 'sha256:worker-build-ppc64le-digest',
                        'tag': 'worker-build-ppc64le-latest',
                        'registry': 'worker-build-ppc64le-registry',
                        'repository': 'worker-build-ppc64le-repository',
                    },
                ],
                'plugins-metadata': {}
            },
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
    })

    assert (build_result.labels == {'koji-build-id': 'koji-build-id'})


def test_orchestrate_build_cancelation(tmpdir):
    workflow = mock_workflow(tmpdir)
    mock_osbs()
    mock_reactor_config(tmpdir)

    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                'platforms': ['x86_64'],
                'build_kwargs': make_worker_build_kwargs(),
                'osbs_client_config': str(tmpdir),
            }
        }]
    )

    def mock_wait_for_build_to_finish(build_name):
        return make_build_response(build_name, 'Running')
    (flexmock(OSBS)
        .should_receive('wait_for_build_to_finish')
        .replace_with(mock_wait_for_build_to_finish))

    flexmock(OSBS).should_receive('cancel_build').once()

    (flexmock(AsyncResult).should_receive('ready')
        .and_return(False)  # normal execution
        .and_return(False)  # after cancel_build
        .and_return(True))  # finally succeed

    class RaiseOnce(object):
        """
        Only raise an exception the first time this mocked wait() method
        is called.
        """

        def __init__(self):
            self.exception_raised = False

        def wait(self, timeout=None):
            time.sleep(0.1)
            if not self.exception_raised:
                self.exception_raised = True
                raise BuildCanceledException()

    raise_once = RaiseOnce()
    (flexmock(AsyncResult).should_receive('wait')
        .replace_with(raise_once.wait))

    # Required due to python3 implementation.
    (flexmock(AsyncResult).should_receive('get')
        .and_return(None))

    with pytest.raises(PluginFailedException) as exc:
        build_result = runner.run()
    assert 'BuildCanceledException' in str(exc)


@pytest.mark.parametrize(('clusters_x86_64'), (
    ([('chosen_x86_64', 5), ('spam', 4)]),
    ([('chosen_x86_64', 5000), ('spam', 4)]),
    ([('spam', 4), ('chosen_x86_64', 5)]),
    ([('chosen_x86_64', 5), ('spam', 4), ('bacon', 4)]),
    ([('chosen_x86_64', 5), ('spam', 5)]),
    ([('chosen_x86_64', 1), ('spam', 1)]),
    ([('chosen_x86_64', 2), ('spam', 2)]),
))
@pytest.mark.parametrize(('clusters_ppc64le'), (
    ([('chosen_ppc64le', 7), ('eggs', 6)]),
))
def test_orchestrate_build_choose_clusters(tmpdir, clusters_x86_64,
                                           clusters_ppc64le):
    workflow = mock_workflow(tmpdir)
    mock_osbs()  # Current builds is a constant 2

    mock_reactor_config(tmpdir, {
        'x86_64': [
            {'name': cluster[0], 'max_concurrent_builds': cluster[1]}
            for cluster in clusters_x86_64
        ],
        'ppc64le': [
            {'name': cluster[0], 'max_concurrent_builds': cluster[1]}
            for cluster in clusters_ppc64le
        ]
    })

    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                'platforms': ['x86_64', 'ppc64le'],
                'build_kwargs': make_worker_build_kwargs(),
                'osbs_client_config': str(tmpdir),
            }
        }]
    )

    build_result = runner.run()
    assert not build_result.is_failed()

    annotations = build_result.annotations
    assert set(annotations['worker-builds'].keys()) == set(['x86_64', 'ppc64le'])
    for platform, plat_annotations in annotations['worker-builds'].items():
        assert plat_annotations['build']['cluster-url'] == 'https://chosen_{}.com/'.format(platform)


def test_orchestrate_build_exclude_platforms(tmpdir):
    workflow = mock_workflow(tmpdir)
    mock_osbs()

    reactor_config = {
        'x86_64': [
            {
                'name': 'worker01',
                'max_concurrent_builds': 3
            }
        ]
    }

    for exclude in ('spam', 'bacon', 'eggs'):
        reactor_config[exclude] = [
            {'name': 'worker-{}'.format(exclude), 'max_concurrent_builds': 3}
        ]

    mock_reactor_config(tmpdir, reactor_config)

    with open(os.path.join(str(tmpdir), 'exclude-platform'), 'w') as f:
        f.write(dedent("""\
            spam

            bacon
            eggs
            """))

    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                # Explicitly leaving off 'eggs' platform to
                # ensure no errors occur when unknow platform
                # is provided in exclude-platform file.
                'platforms': ['x86_64', 'spam', 'bacon'],
                'build_kwargs': make_worker_build_kwargs(),
                'osbs_client_config': str(tmpdir),
            }
        }]
    )

    build_result = runner.run()
    assert not build_result.is_failed()

    annotations = build_result.annotations
    assert set(annotations['worker-builds'].keys()) == set(['x86_64'])


def test_orchestrate_build_unknown_platform(tmpdir):
    workflow = mock_workflow(tmpdir)
    mock_osbs()
    mock_reactor_config(tmpdir)

    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                # Explicitly leaving off 'eggs' platform to
                # ensure no errors occur when unknow platform
                # is provided in exclude-platform file.
                'platforms': ['x86_64', 'spam'],
                'build_kwargs': make_worker_build_kwargs(),
                'osbs_client_config': str(tmpdir),
            }
        }]
    )

    with pytest.raises(PluginFailedException) as exc:
        runner.run()
    assert "'No clusters found for platform spam!'" in str(exc)


@pytest.mark.parametrize('fail_at', ('create', 'wait_to_finish'))
def test_orchestrate_build_failed_create(tmpdir, fail_at):
    workflow = mock_workflow(tmpdir)
    mock_osbs()

    if fail_at == 'create':
        def mock_create_worker_build(**kwargs):
            if kwargs['platform'] == 'ppc64le':
                raise OsbsException('it happens')
            return make_build_response('worker-build-1', 'Running')
        (flexmock(OSBS)
            .should_receive('create_worker_build')
            .replace_with(mock_create_worker_build))
        fail_reason = 'build not started'
        annotation_keys = set(['x86_64'])

    elif fail_at == 'wait_to_finish':
        def mock_wait_for_build_to_finish(build_name):
            if build_name == 'worker-build-ppc64le':
                raise OsbsException('it happens')
            return make_build_response(build_name, 'Complete')
        (flexmock(OSBS)
            .should_receive('wait_for_build_to_finish')
            .replace_with(mock_wait_for_build_to_finish))
        fail_reason = "'it happens'"
        annotation_keys = set(['x86_64', 'ppc64le'])

    else:
        raise ValueError('Invalid value for fail_at: {}'.format(fail_at))

    mock_reactor_config(tmpdir)

    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                'platforms': ['x86_64', 'ppc64le'],
                'build_kwargs': make_worker_build_kwargs(),
                'osbs_client_config': str(tmpdir),
            }
        }]
    )

    build_result = runner.run()
    assert build_result.is_failed()

    annotations = build_result.annotations
    assert set(annotations['worker-builds'].keys()) == annotation_keys
    assert fail_reason in json.loads(build_result.fail_reason)['ppc64le']['general']
