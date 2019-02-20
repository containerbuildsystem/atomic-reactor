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
                                                            get_koji_upload_dir,
                                                            override_build_kwarg)
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfig,
                                                       ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY)
from atomic_reactor.plugins.pre_check_and_set_rebuild import CheckAndSetRebuildPlugin
from atomic_reactor.util import ImageName, df_parser, DigestCollector
import atomic_reactor.util
from atomic_reactor.constants import PLUGIN_ADD_FILESYSTEM_KEY, PLUGIN_CHECK_AND_SET_PLATFORMS_KEY
from flexmock import flexmock
from multiprocessing.pool import AsyncResult
from osbs.api import OSBS
from osbs.conf import Configuration
from osbs.build.build_response import BuildResponse
from osbs.exceptions import OsbsException
from osbs.core import Openshift
from tests.constants import MOCK_SOURCE, TEST_IMAGE, INPUT_IMAGE, SOURCE
from tests.docker_mock import mock_docker
from textwrap import dedent
from copy import deepcopy

import json
import os
import sys
import pytest
import time
import platform


MANIFEST_LIST = {
    'manifests': [
        {'platform': {'architecture': 'amd64'}, 'digest': 'sha256:123456'},
        {'platform': {'architecture': 'ppc64le'}, 'digest': 'sha256:123456'},
    ]
}


DEFAULT_CLUSTERS = {
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


class MockSource(object):

    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = os.path.join(tmpdir, 'Dockerfile')
        self.path = tmpdir
        self.config = flexmock(image_build_method=None)

    def get_build_file_path(self):
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
        self.parent_images_digests = DigestCollector()

        def simplegen(x, y):
            yield "some\u2018".encode('utf-8')
        flexmock(self.tasker, build_image_from_path=simplegen)

    def get_built_image_info(self):
        return {'Id': 'some'}

    def inspect_built_image(self):
        return None

    def ensure_not_built(self):
        pass


class fake_imagestream_tag(object):
    def __init__(self, json_cont):
        self.json_cont = json_cont

    def json(self):
        return self.json_cont


class fake_manifest_list(object):
    def __init__(self, json_cont):
        self.content = json_cont

    def json(self):
        return self.content


def mock_workflow(tmpdir, platforms=['x86_64', 'ppc64le']):
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

    workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = set(platforms)

    build = {
        "spec": {
            "strategy": {
                "customStrategy": {
                    "from": {"name": "registry/some_image@sha256:123456",
                             "kind": "DockerImage"}}}},
        "metadata": {
            "annotations": {
                "from": json.dumps({"name": "registry/some_image:latest",
                                    "kind": "DockerImage"})}}}
    flexmock(os, environ={'BUILD': json.dumps(build)})

    return workflow


def mock_reactor_config(tmpdir, clusters=None, empty=False, add_config=None):
    if not clusters and not empty:
        clusters = deepcopy(DEFAULT_CLUSTERS)

    conf_json = {'version': 1, 'clusters': clusters}
    if add_config:
        conf_json.update(add_config)
    conf = ReactorConfig(conf_json)
    (flexmock(pre_reactor_config)
        .should_receive('get_config')
        .and_return(conf))

    with open(os.path.join(str(tmpdir), 'osbs.conf'), 'w') as f:
        for plat, plat_clusters in clusters.items():
            for cluster in plat_clusters:
                f.write(dedent("""\
                    [{name}]
                    openshift_url = https://{name}.com/
                    namespace = {name}_namespace
                    """.format(name=cluster['name'])))
    return conf_json


def mock_manifest_list():
    (flexmock(atomic_reactor.util)
     .should_receive('get_manifest_list')
     .and_return(fake_manifest_list(MANIFEST_LIST)))


def mock_orchestrator_platfrom(plat='x86_64'):
    (flexmock(platform)
     .should_receive('processor')
     .and_return(plat))


def mock_osbs(current_builds=2, worker_builds=1, logs_return_bytes=False, worker_expect=None):
    (flexmock(OSBS)
        .should_receive('list_builds')
        .and_return(range(current_builds)))

    koji_upload_dirs = set()

    def mock_create_worker_build(**kwargs):
        # koji_upload_dir parameter must be identical for all workers
        koji_upload_dirs.add(kwargs.get('koji_upload_dir'))
        assert len(koji_upload_dirs) == 1

        if worker_expect:
            testkwargs = deepcopy(kwargs)
            testkwargs.pop('koji_upload_dir')
            assert testkwargs == worker_expect

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
        'user': 'bacon',
        'arrangement_version': 6
    }
    kwargs.update(overrides)
    return kwargs


def teardown_function(function):
    sys.modules.pop('build_orchestrate_build', None)


@pytest.mark.parametrize('config_kwargs', [
    None,
    {},
    {'build_image': 'osbs-buildroot:latest'},
    {'build_image': 'osbs-buildroot:latest', 'sources_command': 'fedpkg source'},
    {'build_image': 'osbs-buildroot:latest',
     'equal_labels': 'label1:label2,label3:label4'},
])
@pytest.mark.parametrize('worker_build_image', [
    'fedora:latest',
    None
])
@pytest.mark.parametrize('logs_return_bytes', [
    True,
    False
])
def test_orchestrate_build(tmpdir, caplog, config_kwargs,
                           worker_build_image, logs_return_bytes, reactor_config_map):
    workflow = mock_workflow(tmpdir, platforms=['x86_64'])
    mock_osbs(logs_return_bytes=logs_return_bytes)
    plugin_args = {
        'platforms': ['x86_64'],
        'build_kwargs': make_worker_build_kwargs(),
        'osbs_client_config': str(tmpdir),
    }
    if worker_build_image:
        plugin_args['worker_build_image'] = worker_build_image
    if config_kwargs is not None:
        plugin_args['config_kwargs'] = config_kwargs

    expected_kwargs = {
        'conf_section': str('worker_x86_64'),
        'conf_file': str(tmpdir) + '/osbs.conf',
        'sources_command': None,
    }
    if config_kwargs:
        expected_kwargs['sources_command'] = config_kwargs.get('sources_command')
        if 'equal_labels' in config_kwargs:
            expected_kwargs['equal_labels'] = config_kwargs.get('equal_labels')

    clusters = deepcopy(DEFAULT_CLUSTERS)

    if reactor_config_map:
        reactor_dict = {'version': 1, 'arrangement_version': 6}
        if config_kwargs and 'sources_command' in config_kwargs:
            reactor_dict['sources_command'] = 'fedpkg source'

        expected_kwargs['source_registry_uri'] = None
        reactor_dict['koji'] = {
            'hub_url': '/',
            'root_url': ''}
        expected_kwargs['koji_hub'] = reactor_dict['koji']['hub_url']
        expected_kwargs['koji_root'] = reactor_dict['koji']['root_url']
        reactor_dict['odcs'] = {'api_url': 'odcs_url'}
        expected_kwargs['odcs_insecure'] = False
        expected_kwargs['odcs_url'] = reactor_dict['odcs']['api_url']
        reactor_dict['pulp'] = {'name': 'pulp_name'}
        expected_kwargs['pulp_registry_name'] = reactor_dict['pulp']['name']
        reactor_dict['prefer_schema1_digest'] = False
        expected_kwargs['prefer_schema1_digest'] = reactor_dict['prefer_schema1_digest']
        reactor_dict['smtp'] = {
            'from_address': 'from',
            'host': 'smtp host'}
        expected_kwargs['smtp_host'] = reactor_dict['smtp']['host']
        expected_kwargs['smtp_from'] = reactor_dict['smtp']['from_address']
        expected_kwargs['smtp_email_domain'] = None
        expected_kwargs['smtp_additional_addresses'] = ""
        expected_kwargs['smtp_error_addresses'] = ""
        expected_kwargs['smtp_to_submitter'] = False
        expected_kwargs['smtp_to_pkgowner'] = False
        reactor_dict['artifacts_allowed_domains'] = ('domain1', 'domain2')
        expected_kwargs['artifacts_allowed_domains'] =\
            ','.join(reactor_dict['artifacts_allowed_domains'])
        reactor_dict['yum_proxy'] = 'yum proxy'
        expected_kwargs['yum_proxy'] = reactor_dict['yum_proxy']
        reactor_dict['content_versions'] = ['v2']
        expected_kwargs['registry_api_versions'] = 'v2'

        # Move client config from plugin args to reactor config
        reactor_dict['clusters_client_config_dir'] = plugin_args.pop('osbs_client_config')

        if config_kwargs and 'equal_labels' in config_kwargs:
            expected_kwargs['equal_labels'] = config_kwargs['equal_labels']

            label_groups = [x.strip() for x in config_kwargs['equal_labels'].split(',')]

            equal_labels = []
            for label_group in label_groups:
                equal_labels.append([label.strip() for label in label_group.split(':')])

            reactor_dict['image_equal_labels'] = equal_labels

        reactor_dict['clusters'] = clusters
        reactor_dict['platform_descriptors'] = [{'platform': 'x86_64',
                                                 'architecture': 'amd64'}]

        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig(reactor_dict)
    else:
        reactor_dict = {'version': 1, 'clusters': clusters}
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig(reactor_dict)

        expected_kwargs['smtp_host'] = None
        expected_kwargs['odcs_insecure'] = None
        expected_kwargs['koji_hub'] = None
        expected_kwargs['smtp_email_domain'] = None
        expected_kwargs['smtp_from'] = None
        expected_kwargs['registry_api_versions'] = ''
        expected_kwargs['source_registry_uri'] = None
        expected_kwargs['yum_proxy'] = None
        expected_kwargs['odcs_url'] = None
        expected_kwargs['smtp_additional_addresses'] = ''
        expected_kwargs['koji_root'] = None
        expected_kwargs['smtp_error_addresses'] = ''
        expected_kwargs['smtp_to_submitter'] = None
        expected_kwargs['artifacts_allowed_domains'] = ''
        expected_kwargs['smtp_to_pkgowner'] = None
        expected_kwargs['prefer_schema1_digest'] = None
        expected_kwargs['pulp_registry_name'] = None

    with open(os.path.join(str(tmpdir), 'osbs.conf'), 'w') as f:
        for plat, plat_clusters in clusters.items():
            for cluster in plat_clusters:
                f.write(dedent("""\
                    [{name}]
                    openshift_url = https://{name}.com/
                    namespace = {name}_namespace
                    """.format(name=cluster['name'])))

    goarch = {'x86_64': 'amd64'}
    plugin_args['goarch'] = goarch
    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': plugin_args
        }]
    )

    # Update with config_kwargs last to ensure that, when set
    # always has precedence over worker_build_image param.
    if config_kwargs is not None:
        expected_kwargs.update(config_kwargs)
    expected_kwargs['build_image'] = 'registry/some_image@sha256:123456'

    (flexmock(atomic_reactor.plugins.pre_reactor_config)
     .should_receive('get_openshift_session')
     .and_return(None))

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

    for record in caplog.records:
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
    mock_manifest_list()

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
                'max_cluster_fails': 2,
                'unreachable_cluster_retry_delay': .1,
                'goarch': {'x86_64': 'amd64'},
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


def test_orchestrate_choose_cluster_retry(tmpdir):

    mock_osbs()
    mock_manifest_list()

    (flexmock(OSBS).should_receive('list_builds')
        .and_raise(OsbsException)
        .and_raise(OsbsException)
        .and_return([1, 2, 3]))

    workflow = mock_workflow(tmpdir)

    mock_reactor_config(tmpdir, {
        'x86_64': [
            {'name': cluster[0], 'max_concurrent_builds': cluster[1]}
            for cluster in [('chosen_x86_64', 5), ('spam', 4)]
        ],
        'ppc64le': [
            {'name': cluster[0], 'max_concurrent_builds': cluster[1]}
            for cluster in [('chosen_ppc64le', 5), ('ham', 5)]
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
                'find_cluster_retry_delay': .1,
                'max_cluster_fails': 2,
                'goarch': {'x86_64': 'amd64'},
            }
        }]
    )

    runner.run()


def test_orchestrate_choose_cluster_retry_timeout(tmpdir):

    mock_manifest_list()
    (flexmock(OSBS).should_receive('list_builds')
        .and_raise(OsbsException)
        .and_raise(OsbsException)
        .and_raise(OsbsException))

    workflow = mock_workflow(tmpdir)

    mock_reactor_config(tmpdir, {
        'x86_64': [
            {'name': cluster[0], 'max_concurrent_builds': cluster[1]}
            for cluster in [('chosen_x86_64', 5), ('spam', 4)]
        ],
        'ppc64le': [
            {'name': cluster[0], 'max_concurrent_builds': cluster[1]}
            for cluster in [('chosen_ppc64le', 5), ('ham', 5)]
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
                'find_cluster_retry_delay': .1,
                'max_cluster_fails': 2,
                'goarch': {'x86_64': 'amd64'},
            }
        }]
    )

    build_result = runner.run()
    assert build_result.is_failed()
    fail_reason = json.loads(build_result.fail_reason)['ppc64le']['general']
    assert 'Could not find appropriate cluster for worker build.' in fail_reason


def test_orchestrate_build_cancelation(tmpdir):
    workflow = mock_workflow(tmpdir, platforms=['x86_64'])
    mock_osbs()
    mock_manifest_list()
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
                'goarch': {'x86_64': 'amd64'},
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

        def get(self, timeout=None):
            time.sleep(0.1)
            if not self.exception_raised:
                self.exception_raised = True
                raise BuildCanceledException()

    raise_once = RaiseOnce()
    (flexmock(AsyncResult).should_receive('get')
        .replace_with(raise_once.get))

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
    mock_manifest_list()

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
                'goarch': {'x86_64': 'amd64'},
            }
        }]
    )

    build_result = runner.run()
    assert not build_result.is_failed()

    annotations = build_result.annotations
    assert set(annotations['worker-builds'].keys()) == set(['x86_64', 'ppc64le'])
    for plat, plat_annotations in annotations['worker-builds'].items():
        assert plat_annotations['build']['cluster-url'] == 'https://chosen_{}.com/'.format(plat)


# This test tests code paths that can no longer be hit in actual operation since
# we exclude platforms with no clusters in check_and_set_platforms.
def test_orchestrate_build_unknown_platform(tmpdir, reactor_config_map):  # noqa
    workflow = mock_workflow(tmpdir, platforms=['x86_64', 'spam'])
    mock_osbs()
    mock_manifest_list()
    if reactor_config_map:
        mock_reactor_config(tmpdir)
    else:
        mock_reactor_config(tmpdir, clusters={}, empty=True)

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
                'goarch': {'x86_64': 'amd64'},
            }
        }]
    )

    with pytest.raises(PluginFailedException) as exc:
        runner.run()
    if reactor_config_map:
        assert "'No clusters found for platform spam!'" in str(exc)
    else:
        count = 0
        if "'No clusters found for platform x86_64!'" in str(exc):
            count += 1
        if "'No clusters found for platform spam!'" in str(exc):
            count += 1
        assert count > 0


def test_orchestrate_build_failed_create(tmpdir):
    workflow = mock_workflow(tmpdir)
    mock_osbs()
    mock_manifest_list()

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
                'find_cluster_retry_delay': .1,
                'failure_retry_delay': .1,
                'goarch': {'x86_64': 'amd64'},
            }
        }]
    )

    build_result = runner.run()
    assert build_result.is_failed()

    annotations = build_result.annotations
    assert set(annotations['worker-builds'].keys()) == annotation_keys
    fail_reason = json.loads(build_result.fail_reason)['ppc64le']['general']
    assert "Could not find appropriate cluster for worker build." in fail_reason


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
    mock_manifest_list()

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
                'goarch': {'x86_64': 'amd64'},
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
    workflow = mock_workflow(tmpdir, platforms=['x86_64'])
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


@pytest.mark.parametrize('fail_at', ('all', 'first'))
def test_orchestrate_build_failed_to_list_builds(tmpdir, fail_at):
    workflow = mock_workflow(tmpdir, platforms=['x86_64'])
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

    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                'platforms': ['x86_64'],
                'build_kwargs': make_worker_build_kwargs(),
                'osbs_client_config': str(tmpdir),
                'find_cluster_retry_delay': .1,
                'max_cluster_fails': 2
            }
        }]
    )
    if fail_at == 'first':
        build_result = runner.run()
        assert not build_result.is_failed()

        annotations = build_result.annotations
        assert annotations['worker-builds']['x86_64']['build']['cluster-url'] == 'https://eggs.com/'
    else:
        build_result = runner.run()
        assert build_result.is_failed()
        if fail_at == 'all':
            assert 'Could not find appropriate cluster for worker build.' \
                in build_result.fail_reason


@pytest.mark.parametrize('is_auto', [
    True,
    False
])
def test_orchestrate_build_worker_build_kwargs(tmpdir, caplog, is_auto):
    workflow = mock_workflow(tmpdir, platforms=['x86_64'])
    expected_kwargs = {
        'git_uri': SOURCE['uri'],
        'git_ref': 'master',
        'git_branch': 'master',
        'user': 'bacon',
        'is_auto': is_auto,
        'platform': 'x86_64',
        'release': '10',
        'arrangement_version': 6,
        'parent_images_digests': {},
        'operator_manifests_extract_platform': 'x86_64',
    }

    reactor_config_override = mock_reactor_config(tmpdir)
    reactor_config_override['openshift'] = {
        'auth': {'enable': None},
        'build_json_dir': None,
        'insecure': False,
        'url': 'https://worker_x86_64.com/'
    }
    expected_kwargs['reactor_config_override'] = reactor_config_override
    mock_osbs(worker_expect=expected_kwargs)

    plugin_args = {
        'platforms': ['x86_64'],
        'build_kwargs': make_worker_build_kwargs(),
        'worker_build_image': 'fedora:latest',
        'osbs_client_config': str(tmpdir),
    }

    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': plugin_args,
        }]
    )
    workflow.prebuild_results[CheckAndSetRebuildPlugin.key] = is_auto

    build_result = runner.run()
    assert not build_result.is_failed()


@pytest.mark.parametrize('overrides', [
    {None: '4242'},
    {'x86_64': '4242'},
    {'x86_64': '4242', None: '1111'},
])
def test_orchestrate_override_build_kwarg(tmpdir, overrides):
    workflow = mock_workflow(tmpdir, platforms=['x86_64'])
    expected_kwargs = {
        'git_uri': SOURCE['uri'],
        'git_ref': 'master',
        'git_branch': 'master',
        'user': 'bacon',
        'is_auto': False,
        'platform': 'x86_64',
        'release': '4242',
        'arrangement_version': 6,
        'parent_images_digests': {},
        'operator_manifests_extract_platform': 'x86_64',
    }
    reactor_config_override = mock_reactor_config(tmpdir)
    reactor_config_override['openshift'] = {
        'auth': {'enable': None},
        'build_json_dir': None,
        'insecure': False,
        'url': 'https://worker_x86_64.com/'
    }
    expected_kwargs['reactor_config_override'] = reactor_config_override
    mock_osbs(worker_expect=expected_kwargs)

    plugin_args = {
        'platforms': ['x86_64'],
        'build_kwargs': make_worker_build_kwargs(),
        'worker_build_image': 'fedora:latest',
        'osbs_client_config': str(tmpdir),
    }

    for plat, value in overrides.items():
        override_build_kwarg(workflow, 'release', value, plat)

    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': plugin_args,
        }]
    )

    build_result = runner.run()
    assert not build_result.is_failed()


@pytest.mark.parametrize('enable_v1', [
    True,
    False
])
@pytest.mark.parametrize('content_versions', [
    ['v1', 'v2'],
    ['v1'],
    ['v2'],
])
def test_orchestrate_override_content_versions(tmpdir, caplog, enable_v1, content_versions):
    workflow = mock_workflow(tmpdir, platforms=['x86_64'])
    expected_kwargs = {
        'git_uri': SOURCE['uri'],
        'git_ref': 'master',
        'git_branch': 'master',
        'user': 'bacon',
        'is_auto': False,
        'platform': 'x86_64',
        'release': '10',
        'arrangement_version': 6,
        'parent_images_digests': {},
        'operator_manifests_extract_platform': 'x86_64',
    }
    add_config = {
        'platform_descriptors': [{
            'platform': 'x86_64',
            'architecture': 'amd64',
            'enable_v1': enable_v1
        }],
        'content_versions': content_versions
    }

    reactor_config_override = mock_reactor_config(tmpdir, add_config=add_config)
    reactor_config_override['openshift'] = {
        'auth': {'enable': None},
        'build_json_dir': None,
        'insecure': False,
        'url': 'https://worker_x86_64.com/'
    }

    will_fail = False
    if not enable_v1 and 'v2' not in content_versions:
        will_fail = True
    if not enable_v1 and 'v1' in content_versions:
        reactor_config_override['content_versions'].remove('v1')

    expected_kwargs['reactor_config_override'] = reactor_config_override
    mock_osbs(worker_expect=expected_kwargs)

    plugin_args = {
        'platforms': ['x86_64'],
        'build_kwargs': make_worker_build_kwargs(),
        'worker_build_image': 'fedora:latest',
        'osbs_client_config': str(tmpdir),
    }

    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': plugin_args,
        }]
    )

    build_result = runner.run()
    if will_fail:
        assert build_result.is_failed()
        assert 'failed to create worker build' in caplog.text
        assert 'content_versions is empty' in caplog.text
    else:
        assert not build_result.is_failed()


@pytest.mark.parametrize(('build', 'exc_str', 'bc', 'bc_cont', 'ims', 'ims_cont',
                          'ml', 'ml_cont'), [
    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name_wrong": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}}},
     "Build object is malformed, failed to fetch buildroot image",
     None, None, None, None, None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind_wrong": "DockerImage"}}}}},
     "Build object is malformed, failed to fetch buildroot image",
     None, None, None, None, None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "wrong_kind"}}}}},
     "Build kind isn't 'DockerImage' but",
     None, None, None, None, None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "status": {
            "config": {"kind": "wrong"}}},
     "Build config type isn't BuildConfig :",
     None, None, None, None, None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "metadata": {"annotations": {}}},
     "Build wasn't created from BuildConfig and neither has 'from'" +
     " annotation, which is needed for specified arch",
     None, None, None, None, None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "metadata": {
            "annotations": {
                "from": json.dumps({"kind": "wrong"})}}},
     "Build annotation has unknown 'kind'",
     None, None, None, None, None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "metadata": {
            "annotations": {
                "from": json.dumps({"kind": "DockerImage",
                                    "name": "registry/image@sha256:123456"})}}},
     "Buildroot image isn't manifest list, which is needed for specified arch",
     None, None, None, None, False, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "status": {
            "config": {"kind": "BuildConfig",
                       "name": "wrong build config"}}},
     "Build config not found :",
     False, None, None, None, None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "status": {
            "config": {"kind": "BuildConfig",
                       "name": "build config"}}},
     "BuildConfig object is malformed",
     True, {"spec": {"strategy": {"customStrategy": {}}}}, None, None,
     None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "status": {
            "config": {"kind": "BuildConfig", "name": "build config"}}},
     "BuildConfig object has unknown 'kind'",
     True, {"spec": {"strategy": {"customStrategy": {"from": {"kind": "wrong_kind"}}}}},
     None, None, None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "status": {
            "config": {"kind": "BuildConfig",
                       "name": "build config"}}},
     "ImageStreamTag not found",
     True, {"spec": {"strategy": {"customStrategy": {"from": {"kind": "ImageStreamTag",
                                                              "name": "wrong_ims"}}}}},
     False, None, None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "status": {
            "config": {"kind": "BuildConfig",
                       "name": "build config"}}},
     "ImageStreamTag is malformed",
     True, {"spec": {"strategy": {"customStrategy": {"from": {"kind": "ImageStreamTag",
                                                              "name": "ims"}}}}},
     True, {"image": {}}, None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "status": {
            "config": {"kind": "BuildConfig",
                       "name": "build config"}}},
     "Image in imageStreamTag 'ims' is missing Labels",
     True, {"spec": {"strategy": {"customStrategy": {"from": {"kind": "ImageStreamTag",
                                                              "name": "ims"}}}}},
     True, {"image": {"dockerImageReference": "some@sha256:12345",
                      "dockerImageMetadata": {"Config": {}}}},
     None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "metadata": {
            "annotations": {
                "from": json.dumps({"kind": "DockerImage",
                                    "name": "registry/image:tag"})}}},
     "Buildroot image isn't manifest list, which is needed for specified arch",
     None, None, None, None, False, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "metadata": {
            "annotations": {
                "from": json.dumps({"kind": "DockerImage",
                                    "name": "registry/image:tag"})}}},
     "Platform for orchestrator 'x86_64' isn't in manifest list",
     None, None, None, None, True, {"manifests": [{"platform": {"architecture": "ppc64le"},
                                                   "digest": "some_image"}]}),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot@sha256:1949494494",
                         "kind": "DockerImage"}}}},
        "metadata": {
            "annotations": {
                "from": json.dumps({"kind": "DockerImage",
                                    "name": "registry/image:tag"})}}},
     "Orchestrator is using image digest 'osbs-buildroot@sha256:1949494494' " +
     "which isn't in manifest list",
     None, None, None, None, True, {"manifests": [{"platform": {"architecture": "amd64"},
                                                   "digest": "some_image"}]}),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "registry/image@osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "metadata": {
            "annotations": {
                "from": json.dumps({"kind": "DockerImage",
                                    "name": "registry/image:tag"})}}},
     "build_image for platform 'ppc64le' not available",
     None, None, None, None, True, {"manifests": [{"platform": {"architecture": "amd64"},
                                                   "digest": "osbs-buildroot:latest"}]}),
])
def test_set_build_image_raises(tmpdir, build, exc_str, bc, bc_cont, ims, ims_cont, ml, ml_cont):
    build = json.dumps(build)
    workflow = mock_workflow(tmpdir)

    orchestrator_default_platform = 'x86_64'
    (flexmock(platform)
     .should_receive('processor')
     .and_return(orchestrator_default_platform))

    flexmock(os, environ={'BUILD': build})
    mock_osbs()
    mock_reactor_config(tmpdir)

    if bc is False:
        (flexmock(Openshift)
         .should_receive('get_build_config')
         .and_raise(OsbsException))
    if bc is True:
        (flexmock(Openshift)
         .should_receive('get_build_config')
         .and_return(bc_cont))
    if ims is False:
        (flexmock(Openshift)
         .should_receive('get_image_stream_tag')
         .and_raise(OsbsException))
    if ims is True:
        (flexmock(Openshift)
         .should_receive('get_image_stream_tag')
         .and_return(fake_imagestream_tag(ims_cont)))
    if ml is False:
        (flexmock(atomic_reactor.util)
         .should_receive('get_manifest_list')
         .and_return(None))
    if ml is True:
        (flexmock(atomic_reactor.util)
         .should_receive('get_manifest_list')
         .and_return(fake_manifest_list(ml_cont)))

    plugin_args = {
        'platforms': ['x86_64', 'ppc64le'],
        'build_kwargs': make_worker_build_kwargs(),
        'worker_build_image': 'osbs-buildroot:latest',
        'osbs_client_config': str(tmpdir),
        'goarch': {'x86_64': 'amd64'},
    }

    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': plugin_args,
        }]
    )

    with pytest.raises(PluginFailedException) as ex:
        runner.run()
    assert "raised an exception: RuntimeError" in str(ex)
    assert exc_str in str(ex)


@pytest.mark.parametrize(('build', 'bc', 'bc_cont', 'ims', 'ims_cont',
                          'ml', 'ml_cont', 'platforms'), [
    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}}},
     None, None, None, None, None, None, ['x86_64']),


    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "registry/osbs-buildroot@sha256:12345",
                         "kind": "DockerImage"}}}},
      "metadata": {
          "annotations": {
              "from": json.dumps({"kind": "ImageStreamTag",
                                  "name": "image_stream_tag"})}}},
     None, None, True,
     {"image": {"dockerImageReference": "registry/osbs-buildroot:ims"}},
     True,
     {"manifests": [{"platform": {"architecture": "ppc64le"},
                     "digest": "sha256:987654321"},
                    {"platform": {"architecture": "amd64"},
                     "digest": "sha256:12345"}]},
     ['ppc64le', 'x86_64']),


    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "registry/osbs-buildroot@sha256:12345",
                         "kind": "DockerImage"}}}},
      "metadata": {
          "annotations": {
              "from": json.dumps({"kind": "ImageStreamTag",
                                  "name": "image_stream_tag"})}}},
     None, None, True,
     {"image": {"dockerImageReference": "registry/osbs-buildroot@sha256:12345",
                "dockerImageMetadata": {
                    "Config": {
                        "Labels": {"release": "1", "version": "1.0"}}}}},
     True,
     {"manifests": [{"platform": {"architecture": "ppc64le"},
                     "digest": "sha256:987654321"},
                    {"platform": {"architecture": "amd64"},
                     "digest": "sha256:12345"}]},
     ['ppc64le', 'x86_64']),


    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "registry/osbs-buildroot@sha256:12345",
                         "kind": "DockerImage"}}}},
      "status": {
          "config": {"kind": "BuildConfig",
                     "name": "build config"}}},
     True,
     {"spec": {"strategy": {"customStrategy": {"from": {"kind": "DockerImage",
                                                        "name": "registry/osbs-buildroot:bc"}}}}},
     False, None, True,
     {"manifests": [{"platform": {"architecture": "ppc64le"},
                     "digest": "sha256:987654321"},
                    {"platform": {"architecture": "amd64"},
                     "digest": "sha256:12345"}]},
     ['ppc64le', 'x86_64']),
])
def test_set_build_image_works(tmpdir, build, bc, bc_cont, ims, ims_cont, ml, ml_cont,
                               platforms):
    build = json.dumps(build)
    workflow = mock_workflow(tmpdir, platforms=platforms)

    orchestrator_default_platform = 'x86_64'
    (flexmock(platform)
     .should_receive('processor')
     .and_return(orchestrator_default_platform))

    flexmock(os, environ={'BUILD': build})
    mock_osbs()
    mock_reactor_config(tmpdir)

    if bc is True:
        (flexmock(Openshift)
         .should_receive('get_build_config')
         .and_return(bc_cont))
    if ims is True:
        (flexmock(Openshift)
         .should_receive('get_image_stream_tag')
         .and_return(fake_imagestream_tag(ims_cont)))
    if ml is True:
        (flexmock(atomic_reactor.util)
         .should_receive('get_manifest_list')
         .and_return(fake_manifest_list(ml_cont)))

    plugin_args = {
        'platforms': platforms,
        'build_kwargs': make_worker_build_kwargs(),
        'worker_build_image': 'osbs-buildroot:latest',
        'osbs_client_config': str(tmpdir),
        'goarch': {'x86_64': 'amd64'},
    }

    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': plugin_args,
        }]
    )

    runner.run()


@pytest.mark.parametrize(('platforms', 'override'), [
    (['ppc64le', 'x86_64'], ['ppc64le']),
    (['ppc64le'], ['ppc64le']),
])
def test_set_build_image_with_override(tmpdir, platforms, override):
    workflow = mock_workflow(tmpdir, platforms=platforms)

    default_build_image = 'registry/osbs-buildroot@sha256:12345'
    build = json.dumps({"spec": {
      "strategy": {
            "customStrategy": {
                "from": {"name": default_build_image, "kind": "DockerImage"}}}},
      "status": {
          "config": {"kind": "BuildConfig", "name": "build config"}}})
    flexmock(os, environ={'BUILD': build})

    mock_osbs()
    mock_manifest_list()
    mock_orchestrator_platfrom()

    build_config = {"spec": {"strategy": {
        "customStrategy": {
            "from": {"kind": "DockerImage",
                     "name": "registry/osbs-buildroot:bc"}}}}}
    (flexmock(Openshift)
     .should_receive('get_build_config')
     .and_return(build_config))

    reactor_config = {
        'version': 1,
        'clusters': deepcopy(DEFAULT_CLUSTERS),
        'platform_descriptors': [{'platform': 'x86_64', 'architecture': 'amd64'}],
        'build_image_override': {plat: 'registry/osbs-buildroot-{}:latest'.format(plat)
                                 for plat in override},
    }

    workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
    workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
        ReactorConfig(reactor_config)

    plugin_args = {
        'platforms': platforms,
        'build_kwargs': make_worker_build_kwargs(),
        'osbs_client_config': str(tmpdir),
    }

    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{'name': OrchestrateBuildPlugin.key, 'args': plugin_args}]
    )

    runner.run()

    for plat in platforms:
        used_build_image = get_worker_build_info(workflow, plat).osbs.build_conf.get_build_image()
        expected_build_image = reactor_config['build_image_override'].get(plat,
                                                                          default_build_image)
        assert used_build_image == expected_build_image


def test_no_platforms(tmpdir):
    workflow = mock_workflow(tmpdir, platforms=[])
    mock_osbs()
    mock_reactor_config(tmpdir)

    (flexmock(OrchestrateBuildPlugin)
     .should_receive('set_build_image')
     .never())

    plugin_args = {
        'platforms': [],
        'build_kwargs': make_worker_build_kwargs(),
        'worker_build_image': 'osbs-buildroot:latest',
        'osbs_client_config': str(tmpdir),
        'goarch': {'x86_64': 'amd64'},
    }

    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': plugin_args,
        }]
    )
    with pytest.raises(PluginFailedException) as exc:
        runner.run()
    assert 'No enabled platform to build on' in str(exc)


@pytest.mark.parametrize('version,warning,exception', (
    (5, "arrangement_version <= 5 is deprecated and will be removed in release 1.6.38", None),
    (6, None, None),
))
def test_orchestrate_build_validate_arrangements(tmpdir, caplog, version, warning, exception):
    workflow = mock_workflow(tmpdir)
    mock_osbs()  # Current builds is a constant 2
    mock_manifest_list()

    mock_reactor_config(tmpdir)

    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                'platforms': ['x86_64', 'ppc64le'],
                'build_kwargs': make_worker_build_kwargs(arrangement_version=version),
                'osbs_client_config': str(tmpdir),
                'goarch': {'x86_64': 'amd64'},
            }
        }]
    )
    if exception:
        with pytest.raises(exception):
            runner.run()
    else:
        runner.run()

    if warning:
        assert warning in caplog.text


def test_parent_images_digests(tmpdir, caplog):
    """Test if digests of parent images are propagated correctly to OSBS
    client"""
    PARENT_IMAGES_DATA = {
        'registry.fedoraproject.org/fedora:latest': {
            'x86_64': 'registry.fedoraproject.org/fedora@sha256:123456789abcdef'
        }
    }

    workflow = mock_workflow(tmpdir, platforms=['x86_64'])
    workflow.builder.parent_images_digests.update_from_dict(PARENT_IMAGES_DATA)
    expected_kwargs = {
        'git_uri': SOURCE['uri'],
        'git_ref': 'master',
        'git_branch': 'master',
        'user': 'bacon',
        'is_auto': False,
        'platform': 'x86_64',
        'release': '10',
        'arrangement_version': 6,
        'parent_images_digests': PARENT_IMAGES_DATA,
        'operator_manifests_extract_platform': 'x86_64',
    }

    reactor_config_override = mock_reactor_config(tmpdir)
    reactor_config_override['openshift'] = {
        'auth': {'enable': None},
        'build_json_dir': None,
        'insecure': False,
        'url': 'https://worker_x86_64.com/'
    }
    expected_kwargs['reactor_config_override'] = reactor_config_override
    mock_osbs(worker_expect=expected_kwargs)

    plugin_args = {
        'platforms': ['x86_64'],
        'build_kwargs': make_worker_build_kwargs(),
        'worker_build_image': 'fedora:latest',
        'osbs_client_config': str(tmpdir),
    }

    runner = BuildStepPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': plugin_args,
        }]
    )

    build_result = runner.run()
    assert not build_result.is_failed()
