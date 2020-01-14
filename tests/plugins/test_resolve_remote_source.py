"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

from textwrap import dedent
import sys

from flexmock import flexmock
import pytest

from atomic_reactor import util
from atomic_reactor.cachito_util import CachitoAPI
from atomic_reactor.constants import PLUGIN_BUILD_ORCHESTRATE_KEY
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.build_orchestrate_build import (
    WORKSPACE_KEY_OVERRIDE_KWARGS, OrchestrateBuildPlugin)
from atomic_reactor.plugins.pre_reactor_config import (
    ReactorConfigPlugin, WORKSPACE_CONF_KEY, ReactorConfig)
from atomic_reactor.plugins.pre_resolve_remote_source import ResolveRemoteSourcePlugin
from atomic_reactor.source import SourceConfig

from tests.constants import TEST_IMAGE
from tests.stubs import StubInsideBuilder, StubSource


CACHITO_URL = 'https://cachito.example.com'
CACHITO_REQUEST_ID = 98765
CACHITO_REQUEST_DOWNLOAD_URL = '{}/api/v1/{}/download'.format(CACHITO_URL, CACHITO_REQUEST_ID)

REMOTE_SOURCE_REPO = 'https://git.example.com/team/repo.git'
REMOTE_SOURCE_REF = 'b55c00f45ec3dfee0c766cea3d395d6e21cc2e5a'


@pytest.fixture
def workflow(tmpdir):
    workflow = DockerBuildWorkflow(
        TEST_IMAGE,
        source={"provider": "git", "uri": "asd"}
    )

    # Stash the tmpdir in workflow so it can be used later
    workflow._tmpdir = tmpdir

    class MockSource(StubSource):

        def __init__(self, workdir):
            super(MockSource, self).__init__()
            self.workdir = workdir

    workflow.source = MockSource(str(tmpdir))

    builder = StubInsideBuilder().for_workflow(workflow)
    builder.set_df_path(str(tmpdir))
    builder.tasker = flexmock()
    workflow.builder = flexmock(builder)
    workflow.buildstep_plugins_conf = [{'name': PLUGIN_BUILD_ORCHESTRATE_KEY}]

    mock_repo_config(workflow)
    mock_reactor_config(workflow)
    mock_cachito_api(workflow)

    return workflow


def mock_reactor_config(workflow, data=None):
    if data is None:
        data = dedent("""\
            version: 1
            cachito:
               api_url: {}
               auth:
                   ssl_certs_dir: {}
            """.format(CACHITO_URL, workflow._tmpdir))

    workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

    workflow._tmpdir.join('cert').write('')
    config = util.read_yaml(data, 'schemas/config.json')

    workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] = ReactorConfig(config)


def mock_repo_config(workflow, data=None):
    if data is None:
        data = dedent("""\
            remote_source:
                repo: {}
                ref: {}
            """.format(REMOTE_SOURCE_REPO, REMOTE_SOURCE_REF))

    workflow._tmpdir.join('container.yaml').write(data)

    # The repo config is read when SourceConfig is initialized. Force
    # reloading here to make usage easier.
    workflow.source.config = SourceConfig(str(workflow._tmpdir))


def mock_cachito_api(workflow, source_request=None):
    if source_request is None:
        source_request = {
            'id': CACHITO_REQUEST_ID,
            'repo': REMOTE_SOURCE_REPO,
            'ref': REMOTE_SOURCE_REF,
            'environment_variables': {
                'GOPATH': 'deps/gomod',
                'GOCACHE': 'deps/gomod',
            },
            'flags': ['enable-confeti', 'enable-party-popper'],
            'pkg_managers': ['gomod'],
            'dependencies': [
                {
                    'name': 'github.com/op/go-logging',
                    'type': 'gomod',
                    'version': 'v0.1.1',
                }
            ],
            'packages': [
                {
                    'name': 'github.com/spam/bacon/v2',
                    'type': 'gomod',
                    'version': 'v2.0.3'
                }
            ],
            'extra_cruft': 'ignored',
        }

    (flexmock(CachitoAPI)
        .should_receive('request_sources')
        .with_args(
            repo=REMOTE_SOURCE_REPO,
            ref=REMOTE_SOURCE_REF,
        )
        .and_return({'id': CACHITO_REQUEST_ID}))

    (flexmock(CachitoAPI)
        .should_receive('wait_for_request')
        .with_args({'id': CACHITO_REQUEST_ID})
        .and_return(source_request))
    (flexmock(CachitoAPI)
        .should_receive('download_sources')
        .with_args(source_request, dest_dir=str(workflow._tmpdir))
        .and_return(expected_dowload_path(workflow)))

    (flexmock(CachitoAPI)
        .should_receive('assemble_download_url')
        .with_args(source_request)
        .and_return(CACHITO_REQUEST_DOWNLOAD_URL))


def expected_dowload_path(workflow):
    return workflow._tmpdir.join('source.tar.gz')


def setup_function(*args):
    # IMPORTANT: This needs to be done to ensure mocks at the module
    # level are reset between test cases.
    sys.modules.pop('pre_resolve_remote_source', None)


def teardown_function(*args):
    # IMPORTANT: This needs to be done to ensure mocks at the module
    # level are reset between test cases.
    sys.modules.pop('pre_resolve_remote_source', None)


def test_resolve_remote_source(workflow):
    run_plugin_with_args(workflow)


@pytest.mark.parametrize('pop_key', ('repo', 'ref'))
def test_invalid_remote_source_structure(workflow, pop_key):
    source_request = {
        'id': CACHITO_REQUEST_ID,
        'repo': REMOTE_SOURCE_REPO,
        'ref': REMOTE_SOURCE_REF,
    }
    source_request.pop(pop_key)
    mock_cachito_api(workflow, source_request=source_request)
    run_plugin_with_args(workflow, expect_error='Received invalid source request')


def test_ignore_when_missing_cachito_config(workflow):
    reactor_config = dedent("""\
        version: 1
        """)
    mock_reactor_config(workflow, reactor_config)
    result = run_plugin_with_args(workflow, expect_result=False)
    assert result is None


def test_invalid_cert_reference(workflow):
    bad_certs_dir = str(workflow._tmpdir.join('invalid-dir'))
    reactor_config = dedent("""\
        version: 1
        cachito:
           api_url: {}
           auth:
               ssl_certs_dir: {}
        """.format(CACHITO_URL, bad_certs_dir))
    mock_reactor_config(workflow, reactor_config)
    run_plugin_with_args(workflow, expect_error="Cachito ssl_certs_dir doesn't exist")


def test_ignore_when_missing_remote_source_config(workflow):
    remote_source_config = dedent("""---""")
    mock_repo_config(workflow, remote_source_config)
    result = run_plugin_with_args(workflow, expect_result=False)
    assert result is None


def run_plugin_with_args(workflow, expect_error=None, expect_result=True):
    runner = PreBuildPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [
            {'name': ResolveRemoteSourcePlugin.key, 'args': {}},
        ]
    )

    if expect_error:
        with pytest.raises(PluginFailedException, match=expect_error):
            runner.run()
        return

    results = runner.run()[ResolveRemoteSourcePlugin.key]

    if expect_result:
        assert results['annotations']['remote_source_url']
        assert results['remote_source_json'] == {
            'repo': REMOTE_SOURCE_REPO,
            'ref': REMOTE_SOURCE_REF,
            'environment_variables': {
                'GOPATH': 'deps/gomod',
                'GOCACHE': 'deps/gomod',
            },
            'flags': ['enable-confeti', 'enable-party-popper'],
            'pkg_managers': ['gomod'],
            'dependencies': [
                {
                    'name': 'github.com/op/go-logging',
                    'type': 'gomod',
                    'version': 'v0.1.1',
                }
            ],
            'packages': [
                {
                    'name': 'github.com/spam/bacon/v2',
                    'type': 'gomod',
                    'version': 'v2.0.3'
                }
            ],
        }
        assert results['remote_source_path'] == expected_dowload_path(workflow)

        # A result means the plugin was enabled and executed successfully.
        # Let's verify the expected side effects.
        orchestrator_build_workspace = workflow.plugin_workspace[OrchestrateBuildPlugin.key]
        worker_params = orchestrator_build_workspace[WORKSPACE_KEY_OVERRIDE_KWARGS][None]
        assert worker_params['remote_source_url'] == CACHITO_REQUEST_DOWNLOAD_URL
        assert worker_params['remote_source_build_args'] == {
            'GOPATH': '/remote-source/deps/gomod',
            'GOCACHE': '/remote-source/deps/gomod',
        }

    return results
