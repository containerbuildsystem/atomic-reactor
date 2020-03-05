"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import, unicode_literals

import pytest

from atomic_reactor.constants import PLUGIN_BUILD_ORCHESTRATE_KEY
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       ReactorConfig,
                                                       WORKSPACE_CONF_KEY)
from atomic_reactor.plugins.pre_pin_operator_digest import PinOperatorDigestsPlugin

from tests.constants import TEST_IMAGE
from tests.stubs import StubInsideBuilder, StubSource, StubConfig


def mock_dockerfile(tmpdir, base='scratch', operator_bundle_label=True):
    dockerfile = (
        'FROM {base}\n'
        'LABEL com.redhat.delivery.operator.bundle={label_value}\n'
    ).format(base=base, label_value=operator_bundle_label)

    tmpdir.join('Dockerfile').write(dockerfile)


def make_reactor_config(operators_config):
    config = {'version': 1}
    config.update(operators_config)
    return ReactorConfig(config)


def make_user_config(operator_config):
    config = StubConfig()
    setattr(config, 'operator_manifest', operator_config)
    return config


def mock_workflow(tmpdir, orchestrator, user_config=None, site_config=None):
    workflow = DockerBuildWorkflow(
        TEST_IMAGE,
        source={'provider': 'git', 'uri': 'asd'}
    )
    workflow.source = StubSource()
    workflow.source.config = make_user_config(user_config)
    workflow.builder = (
        StubInsideBuilder().for_workflow(workflow).set_df_path(str(tmpdir))
    )

    if orchestrator:
        workflow.buildstep_plugins_conf = [{'name': PLUGIN_BUILD_ORCHESTRATE_KEY}]

        workflow.plugin_workspace[ReactorConfigPlugin.key] = {
            WORKSPACE_CONF_KEY: make_reactor_config(site_config or {})
        }

    return workflow


def mock_env(docker_tasker, tmpdir, orchestrator,
             user_config=None, site_config=None,
             df_base='scratch', df_operator_label=True,
             replacement_pullspecs=None):
    """
    Mock environment for test

    :param docker_tasker: conftest fixture
    :param tmpdir: pylint fixture,
    :param orchestrator: is the plugin running in orchestrator?
    :param user_config: container.yaml operator_manifest config
    :param site_config: reactor-config-map operator_manifests config
    :param df_base: base image in Dockerfile, non-scratch should fail
    :param df_operator_label: presence of operator manifest bundle label
    :param replacement_pullspecs: plugin argument from osbs-client

    :return: configured plugin runner
    """
    mock_dockerfile(tmpdir, df_base, df_operator_label)
    workflow = mock_workflow(tmpdir, orchestrator,
                             user_config=user_config, site_config=site_config)

    plugin_conf = [{'name': PinOperatorDigestsPlugin.key,
                    'args': {'replacement_pullspecs': replacement_pullspecs}}]
    runner = PreBuildPluginsRunner(docker_tasker, workflow, plugin_conf)

    return runner


class TestPinOperatorDigest(object):
    def _get_site_config(self, allowed_registries=None):
        return {
            'operator_manifests': {
                'allowed_registries': allowed_registries
            }
        }

    @pytest.mark.parametrize('orchestrator', [True, False])
    def test_run_only_for_operator_bundle_label(self, orchestrator,
                                                docker_tasker, tmpdir, caplog):
        runner = mock_env(docker_tasker, tmpdir,
                          orchestrator=orchestrator, df_operator_label=False)
        runner.run()
        assert "Not an operator manifest bundle build, skipping plugin" in caplog.text

    def test_missing_orchestrator_config(self, docker_tasker, tmpdir):
        runner = mock_env(docker_tasker, tmpdir, orchestrator=True)
        with pytest.raises(PluginFailedException) as exc_info:
            runner.run()
        msg = "operator_manifests configuration missing in reactor config map"
        assert msg in str(exc_info.value)
