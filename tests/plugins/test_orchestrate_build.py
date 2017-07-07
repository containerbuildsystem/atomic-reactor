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
from atomic_reactor.plugins.build_orchestrate_build import (OrchestrateBuildPlugin,
                                                            get_worker_build_info,
                                                            get_koji_upload_dir)
from atomic_reactor.plugins.pre_reactor_config import ReactorConfig
from atomic_reactor.util import ImageName, df_parser
from atomic_reactor.constants import PLUGIN_ADD_FILESYSTEM_KEY
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
import yaml


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
    setattr(builder, 'source', source)
    setattr(workflow, 'source', source)
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

    koji_upload_dirs = set()

    def mock_create_worker_build(**kwargs):
        # koji_upload_dir parameter must be identical for all workers
        koji_upload_dirs.add(kwargs.get('koji_upload_dir'))
        assert len(koji_upload_dirs) == 1

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
def test_orchestrate_build(tmpdir, caplog, config_kwargs, worker_build_image, logs_return_bytes):
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

    build_info = get_worker_build_info(workflow, 'x86_64')
    assert build_info.osbs

    for record in caplog.records():
        if not record.name.startswith("atomic_reactor"):
            continue

        assert hasattr(record, 'arch')
        if record.funcName == 'watch_logs':
            assert record.arch == 'x86_64'
        else:
            assert record.arch == '-'


@pytest.mark.parametrize('metadata_fragment', [
    True,
    False
])
def test_orchestrate_build_annotations_and_labels(tmpdir, metadata_fragment):
    workflow = mock_workflow(tmpdir)
    mock_osbs()

    md = {
        'metadata_fragment': 'configmap/spam-md',
        'metadata_fragment_key': 'metadata.json'
    }

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
        if metadata_fragment:
            annotations.update(md)

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

    expected = {
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
    }
    if metadata_fragment:
        expected['worker-builds']['x86_64'].update(md)
        expected['worker-builds']['ppc64le'].update(md)

    assert (build_result.annotations == expected)

    assert (build_result.labels == {'koji-build-id': 'koji-build-id'})

    build_info = get_worker_build_info(workflow, 'x86_64')
    assert build_info.osbs

    koji_upload_dir = get_koji_upload_dir(workflow)
    assert koji_upload_dir


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
        runner.run()
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


@pytest.mark.parametrize(('platforms', 'platform_exclude', 'platform_only', 'result'), [
    (['x86_64', 'powerpc64le'], '', 'powerpc64le', ['powerpc64le']),
    (['x86_64', 'spam', 'bacon', 'toast', 'powerpc64le'], ['spam', 'bacon', 'eggs', 'toast'], '',
     ['x86_64', 'powerpc64le']),
    (['powerpc64le', 'spam', 'bacon', 'toast'], ['spam', 'bacon', 'eggs', 'toast'], 'powerpc64le',
     ['powerpc64le']),
    (['x86_64', 'bacon', 'toast'], 'toast', ['x86_64', 'powerpc64le'], ['x86_64']),
    (['x86_64', 'toast'], 'toast', 'x86_64', ['x86_64']),
    (['x86_64', 'spam', 'bacon', 'toast'], ['spam', 'bacon', 'eggs', 'toast'], ['x86_64',
                                                                                'powerpc64le'],
     ['x86_64']),
    (['x86_64', 'powerpc64le'], '', '', ['x86_64', 'powerpc64le'])
])
def test_orchestrate_build_exclude_platforms(tmpdir, platforms, platform_exclude, platform_only,
                                             result):
    workflow = mock_workflow(tmpdir)
    mock_osbs()

    reactor_config = {
        'x86_64': [
            {
                'name': 'worker01',
                'max_concurrent_builds': 3
            }
        ],
        'powerpc64le': [
            {
                'name': 'worker02',
                'max_concurrent_builds': 3
            }
        ]
    }

    for exclude in ('spam', 'bacon', 'eggs'):
        reactor_config[exclude] = [
            {'name': 'worker-{}'.format(exclude), 'max_concurrent_builds': 3}
        ]

    mock_reactor_config(tmpdir, reactor_config)

    platforms_dict = {}
    if platform_exclude != '':
        platforms_dict['platforms'] = {}
        platforms_dict['platforms']['not'] = platform_exclude
    if platform_only != '':
        if 'platforms' not in platforms_dict:
            platforms_dict['platforms'] = {}
        platforms_dict['platforms']['only'] = platform_only

    with open(os.path.join(str(tmpdir), 'container.yaml'), 'w') as f:
        f.write(yaml.safe_dump(platforms_dict))
        f.flush()

    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                # Explicitly leaving off 'eggs' platform to
                # ensure no errors occur when unknown platform
                # is provided in container.yaml file.
                'platforms': platforms,
                'build_kwargs': make_worker_build_kwargs(),
                'osbs_client_config': str(tmpdir),
            }
        }]
    )

    build_result = runner.run()
    assert not build_result.is_failed()

    annotations = build_result.annotations
    assert set(annotations['worker-builds'].keys()) == set(result)


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


def test_orchestrate_build_failed_create(tmpdir):
    workflow = mock_workflow(tmpdir)
    mock_osbs()

    def mock_create_worker_build(**kwargs):
        if kwargs['platform'] == 'ppc64le':
            raise OsbsException('it happens')
        return make_build_response('worker-build-1', 'Running')
    (flexmock(OSBS)
     .should_receive('create_worker_build')
     .replace_with(mock_create_worker_build))
    fail_reason = 'build not started'
    annotation_keys = set(['x86_64'])

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


@pytest.mark.parametrize('pod_available,pod_failure_reason,expected,cancel_fails', [
    # get_pod_for_build() returns error
    (False,
     None,
     KeyError,
     False),

    # get_failure_reason() not available in PodResponse
    (True,
     AttributeError("'module' object has no attribute 'get_failure_reason'"),
     KeyError,
     False),

    # get_failure_reason() result used
    (True,
     {
         'reason': 'reason message',
         'exitCode': 23,
         'containerID': 'abc123',
     },
     {
         'reason': 'reason message',
         'exitCode': 23,
         'containerID': 'abc123',
     },
     False),

    # cancel_build() fails (and failure is ignored)
    (True,
     {
         'reason': 'reason message',
         'exitCode': 23,
         'containerID': 'abc123',
     },
     {
         'reason': 'reason message',
         'exitCode': 23,
         'containerID': 'abc123',
     },
     True)
])
def test_orchestrate_build_failed_waiting(tmpdir,
                                          pod_available,
                                          pod_failure_reason,
                                          cancel_fails,
                                          expected):
    workflow = mock_workflow(tmpdir)
    mock_osbs()

    class MockPodResponse(object):
        def __init__(self, pod_failure_reason):
            self.pod_failure_reason = pod_failure_reason

        def get_failure_reason(self):
            if isinstance(self.pod_failure_reason, Exception):
                raise self.pod_failure_reason

            return self.pod_failure_reason

    def mock_wait_for_build_to_finish(build_name):
        if build_name == 'worker-build-ppc64le':
            raise OsbsException('it happens')
        return make_build_response(build_name, 'Failed')
    (flexmock(OSBS)
     .should_receive('wait_for_build_to_finish')
     .replace_with(mock_wait_for_build_to_finish))

    cancel_build_expectation = flexmock(OSBS).should_receive('cancel_build')
    if cancel_fails:
        cancel_build_expectation.and_raise(OsbsException)

    cancel_build_expectation.once()

    expectation = flexmock(OSBS).should_receive('get_pod_for_build')
    if pod_available:
        expectation.and_return(MockPodResponse(pod_failure_reason))
    else:
        expectation.and_raise(OsbsException())

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
    assert set(annotations['worker-builds'].keys()) == {'x86_64', 'ppc64le'}
    fail_reason = json.loads(build_result.fail_reason)['ppc64le']

    if expected is KeyError:
        assert 'pod' not in fail_reason
    else:
        assert fail_reason['pod'] == expected


@pytest.mark.parametrize(('task_id', 'error'), [
    ('1234567', None),
    ('bacon', 'ValueError'),
    (None, 'TypeError'),
])
def test_orchestrate_build_get_fs_task_id(tmpdir, task_id, error):
    workflow = mock_workflow(tmpdir)
    mock_osbs()

    mock_reactor_config(tmpdir)

    workflow.prebuild_results[PLUGIN_ADD_FILESYSTEM_KEY] = {
        'filesystem-koji-task-id': task_id,
    }
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

    if error is not None:
        with pytest.raises(PluginFailedException) as exc:
            runner.run()
        workflow.build_result.is_failed()
        assert error in str(exc)

    else:
        build_result = runner.run()
        assert not build_result.is_failed()


@pytest.mark.parametrize('fail_at', ('all', 'first', 'build_canceled'))
def test_orchestrate_build_failed_to_list_builds(tmpdir, fail_at):
    workflow = mock_workflow(tmpdir)
    mock_osbs()  # Current builds is a constant 2

    mock_reactor_config(tmpdir, {
        'x86_64': [
            {'name': 'spam', 'max_concurrent_builds': 5},
            {'name': 'eggs', 'max_concurrent_builds': 5}
        ],
    })

    flexmock_chain = flexmock(OSBS).should_receive('list_builds').and_raise(OsbsException("foo"))

    if fail_at == 'all':
        flexmock_chain.and_raise(OsbsException("foo"))

    if fail_at == 'first':
        flexmock_chain.and_return(['a', 'b'])

    if fail_at == 'build_canceled':
        flexmock_chain.and_raise(OsbsException(cause=BuildCanceledException()))

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
    if fail_at == 'first':
        build_result = runner.run()
        assert not build_result.is_failed()

        annotations = build_result.annotations
        assert annotations['worker-builds']['x86_64']['build']['cluster-url'] == 'https://eggs.com/'
    else:
        with pytest.raises(PluginFailedException) as exc:
            build_result = runner.run()
        if fail_at == 'all':
            assert 'All clusters for platform x86_64 are unreachable' in str(exc)
        elif fail_at == 'build_canceled':
            assert 'BuildCanceledException()' in str(exc)
