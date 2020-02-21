"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import absolute_import

import os

import flexmock
import pytest

from atomic_reactor import util
from atomic_reactor.constants import (
    PLUGIN_PUSH_OPERATOR_MANIFESTS_KEY,
    PLUGIN_BUILD_ORCHESTRATE_KEY
)
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_check_and_set_rebuild import CheckAndSetRebuildPlugin
from atomic_reactor.plugins.build_orchestrate_build import (
    OrchestrateBuildPlugin,
    WORKSPACE_KEY_UPLOAD_DIR,
)
from atomic_reactor.plugins.pre_reactor_config import (
    ReactorConfigPlugin,
    WORKSPACE_CONF_KEY,
    ReactorConfig,
)
from atomic_reactor.plugins.post_push_operator_manifest import PushOperatorManifestsPlugin
from atomic_reactor.omps_util import OMPS, OMPSError

from tests.constants import TEST_IMAGE
from tests.stubs import StubInsideBuilder, StubSource
from tests.plugins.test_export_operator_manifests import mock_dockerfile


KOJIROOT_TEST_URL = 'http://koji.localhost/kojiroot'
KOJI_UPLOAD_TEST_WORKDIR = 'temp_workdir'

TEST_OMPS_NAMESPACE = 'test_org'
TEST_OMPS_APPREGISTRY = 'https://quay.io./cnr'
TEST_OMPS_REPO = 'test_repo'
TEST_OMPS_VERSION = '0.0.1'


class MockSource(StubSource):

    def __init__(self, workdir):
        super(MockSource, self).__init__()
        self.workdir = workdir

    def get_build_file_path(self):
        return os.path.join(self.workdir, 'Dockerfile'), self.workdir


def mock_workflow(tmpdir, for_orchestrator=False):
    workflow = DockerBuildWorkflow(
        TEST_IMAGE,
        source={"provider": "git", "uri": "asd"},
    )
    workflow.source = MockSource(str(tmpdir))
    builder = StubInsideBuilder().for_workflow(workflow)
    builder.set_df_path(str(tmpdir))
    builder.tasker = flexmock()
    workflow.builder = flexmock(builder)

    if for_orchestrator:
        workflow.buildstep_plugins_conf = [{'name': PLUGIN_BUILD_ORCHESTRATE_KEY}]

    return workflow


def mock_omps(push_fail=False):
    mocked_omps = flexmock()
    if push_fail:
        (mocked_omps
         .should_receive('push_archive')
         .and_raise(OMPSError, 'OMPS failure'))
    else:
        (mocked_omps
         .should_receive('push_archive')
         .and_return({
            'organization': TEST_OMPS_NAMESPACE,
            'repo': TEST_OMPS_REPO,
            'version': TEST_OMPS_VERSION,
         }))
    flexmock(OMPS).should_receive('from_config').and_return(mocked_omps)


def mock_koji_manifest_download(requests_mock):
    url = '{}/work/{}/operator_manifests.zip'.format(
        KOJIROOT_TEST_URL, KOJI_UPLOAD_TEST_WORKDIR)
    requests_mock.register_uri('GET', url, content=b'zip archive')


def mock_env(tmpdir, docker_tasker,
             has_appregistry_label=True, appregistry_label=True,
             has_bundle_label=False, bundle_label=False,
             scratch=False, isolated=False, rebuild=False,
             orchestrator=True, omps_configured=True, omps_push_fail=False):
    build_json = {'metadata': {'labels': {
        'scratch': scratch,
        'isolated': isolated,
    }}}
    flexmock(util).should_receive('get_build_json').and_return(build_json)
    mock_dockerfile(
        tmpdir,
        has_appregistry_label=has_appregistry_label, appregistry_label=appregistry_label,
        has_bundle_label=has_bundle_label, bundle_label=bundle_label,
    )
    workflow = mock_workflow(tmpdir, for_orchestrator=orchestrator)
    if omps_configured:
        omps_map = {
            'omps_url': 'https://localhost',
            'omps_namespace': TEST_OMPS_NAMESPACE,
            'omps_secret_dir': '/var/run/secrets/atomic-reactor/ompssecret',
            'appregistry_url': TEST_OMPS_APPREGISTRY,
        }
    else:
        omps_map = {}

    koji_map = {'root_url': KOJIROOT_TEST_URL}

    mock_omps(push_fail=omps_push_fail)

    if rebuild:
        workflow.prebuild_results[CheckAndSetRebuildPlugin.key] = True

    workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
    workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
        ReactorConfig({'version': 1, 'omps': omps_map, 'koji': koji_map})
    workflow.plugin_workspace[OrchestrateBuildPlugin.key] = {
        WORKSPACE_KEY_UPLOAD_DIR: KOJI_UPLOAD_TEST_WORKDIR
    }
    plugin_conf = [{'name': PushOperatorManifestsPlugin.key}]

    workflow.postbuild_plugins_conf = plugin_conf

    runner = PostBuildPluginsRunner(docker_tasker, workflow, plugin_conf)

    return runner


class TestPushOperatorManifests(object):

    @pytest.mark.parametrize('has_appregistry_label', [True, False])
    @pytest.mark.parametrize('appregistry_label', [True, False])
    @pytest.mark.parametrize('has_bundle_label', [True, False])
    @pytest.mark.parametrize('bundle_label', [True, False])
    @pytest.mark.parametrize('scratch', [True, False])
    @pytest.mark.parametrize('isolated', [True, False])
    @pytest.mark.parametrize('rebuild', [True, False])
    @pytest.mark.parametrize('orchestrator', [True, False])
    @pytest.mark.parametrize('omps_configured', [True, False])
    def test_skip(
        self, requests_mock, tmpdir, docker_tasker, caplog,
        has_appregistry_label, appregistry_label,
        has_bundle_label, bundle_label, scratch,
        isolated, rebuild, orchestrator, omps_configured
    ):
        """Test if plugin execution is skipped in expected cases"""
        mock_koji_manifest_download(requests_mock)
        runner = mock_env(tmpdir, docker_tasker,
                          has_appregistry_label=has_appregistry_label,
                          appregistry_label=appregistry_label,
                          has_bundle_label=has_bundle_label,
                          bundle_label=bundle_label,
                          scratch=scratch,
                          isolated=isolated,
                          rebuild=rebuild,
                          orchestrator=orchestrator,
                          omps_configured=omps_configured)
        should_skip = any([
            not (has_appregistry_label and appregistry_label),
            has_bundle_label and bundle_label,
            scratch, rebuild, isolated,
            not orchestrator, not omps_configured
        ])
        result = runner.run()
        if should_skip:
            assert 'Skipping' in caplog.text
            assert result[PLUGIN_PUSH_OPERATOR_MANIFESTS_KEY] is None
        else:
            assert 'Skipping' not in caplog.text
            assert result[PLUGIN_PUSH_OPERATOR_MANIFESTS_KEY]

    def test_successful_push(self, requests_mock, tmpdir, docker_tasker):
        """Test of plugin output in success run"""
        expected = {
            'endpoint': TEST_OMPS_APPREGISTRY,
            'registryNamespace': TEST_OMPS_NAMESPACE,
            'repository': TEST_OMPS_REPO,
            'release': TEST_OMPS_VERSION,
        }
        mock_koji_manifest_download(requests_mock)
        runner = mock_env(tmpdir, docker_tasker)
        result = runner.run()
        assert result[PLUGIN_PUSH_OPERATOR_MANIFESTS_KEY] == expected

    def test_failed_push(self, requests_mock, tmpdir, docker_tasker):
        """Test of plugin output when OMPS push failed"""
        mock_koji_manifest_download(requests_mock)
        runner = mock_env(tmpdir, docker_tasker, omps_push_fail=True)
        with pytest.raises(PluginFailedException) as exc:
            runner.run()
        assert 'Failed to push operator manifests:' in str(exc.value)
