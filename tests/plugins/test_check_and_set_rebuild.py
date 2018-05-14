"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import pytest
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_check_and_set_rebuild import (is_rebuild,
                                                              CheckAndSetRebuildPlugin)
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor.plugins import build_orchestrate_build
from atomic_reactor.util import ImageName
import json
from osbs.api import OSBS
import osbs.conf
from flexmock import flexmock
from tests.constants import MOCK, MOCK_SOURCE
from tests.fixtures import docker_tasker, reactor_config_map  # noqa
from textwrap import dedent
if MOCK:
    from tests.docker_mock import mock_docker


class MockInsideBuilder(object):
    def __init__(self, tmpdir):
        self.tasker = DockerTasker()
        self.base_image = ImageName(repo='Fedora', tag='22')
        self.image_id = 'image_id'
        self.image = 'image'
        self.source = MockSource(tmpdir)


class MockSource(object):
    def __init__(self, tmpdir):
        self.dockerfile_path = str(tmpdir.join('Dockerfile'))
        self.path = str(tmpdir)
        self.commit_id = None
        self.config = flexmock(autorebuild=dict(from_latest=False))

    def get_build_file_path(self):
        return self.dockerfile_path, self.path

    def reset(self, git_reference):
        self.commit_id = 'HEAD-OF-' + git_reference


class TestCheckRebuild(object):
    def prepare(self, tmpdir, key, value, update_labels_args=None, update_labels_kwargs=None,
                reactor_config_map=False):
        if MOCK:
            mock_docker()
        tasker = DockerTasker()

        workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
        workflow.builder = MockInsideBuilder(tmpdir)
        workflow.source = workflow.builder.source
        flexmock(workflow, base_image_inspect={})

        expectation = (flexmock(OSBS)
                       .should_receive('update_labels_on_build_config'))
        if update_labels_args is not None:
            if update_labels_kwargs is None:
                update_labels_kwargs = {}

            expectation.with_args(*update_labels_args)

        namespace = None
        if update_labels_kwargs is not None:
            namespace = update_labels_kwargs.get('namespace')
        (flexmock(osbs.conf).should_call('Configuration')
         .with_args(namespace=namespace, conf_file=None, verify_ssl=True, openshift_url="",
                    use_auth=True, build_json_dir=None))

        if reactor_config_map:
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1,
                               'openshift': {
                                   'url': '',
                                   'insecure': False,
                                   'auth': {'enable': True}}})

        runner = PreBuildPluginsRunner(tasker, workflow, [
            {
                'name': CheckAndSetRebuildPlugin.key,
                'args': {
                    'label_key': key,
                    'label_value': value,
                    'url': '',
                },
            }
        ])
        return workflow, runner

    def test_check_rebuild_no_build_json(self, tmpdir, monkeypatch, reactor_config_map):
        workflow, runner = self.prepare(tmpdir, 'is_autorebuild', 'true',
                                        reactor_config_map=reactor_config_map)
        monkeypatch.delenv('BUILD', raising=False)

        with pytest.raises(PluginFailedException):
            runner.run()

    def test_check_no_buildconfig(self, tmpdir, monkeypatch):
        key = 'is_autorebuild'
        value = 'true'
        workflow, runner = self.prepare(tmpdir, key, value, reactor_config_map=reactor_config_map)
        monkeypatch.setenv("BUILD", json.dumps({
            "metadata": {
                "labels": {
                    key: value,
                }
            }
        }))

        # No buildconfig in metadata
        with pytest.raises(PluginFailedException):
            runner.run()

    @pytest.mark.parametrize(('namespace'), [None, 'my_namespace'])
    def test_check_is_not_rebuild(self, tmpdir, namespace, monkeypatch, reactor_config_map):
        key = 'is_autorebuild'
        value = 'true'
        buildconfig = "buildconfig1"
        namespace_dict = {}
        if namespace is not None:
            namespace_dict["namespace"] = namespace

        workflow, runner = self.prepare(tmpdir, key, value,
                                        update_labels_args=(buildconfig,
                                                            {key: value}),
                                        update_labels_kwargs=namespace_dict,
                                        reactor_config_map=reactor_config_map)

        build_json = {
            "metadata": {
                "labels": {
                    "buildconfig": buildconfig,
                    key: "false",
                }
            }
        }

        build_json["metadata"].update(namespace_dict)
        monkeypatch.setenv("BUILD", json.dumps(build_json))
        runner.run()
        assert workflow.prebuild_results[CheckAndSetRebuildPlugin.key] is False
        assert not is_rebuild(workflow)

    @pytest.mark.parametrize('from_latest', (None, True, False))
    def test_check_is_rebuild(self, tmpdir, monkeypatch, reactor_config_map, from_latest):
        key = 'is_autorebuild'
        value = 'true'

        workflow, runner = self.prepare(tmpdir, key, value, reactor_config_map=reactor_config_map)

        monkeypatch.setenv("BUILD", json.dumps({
            "metadata": {
                "labels": {
                    "buildconfig": "buildconfig1",
                    key: value,
                    'git-branch': 'the-branch',
                }
            }
        }))

        (flexmock(workflow.source)
            .should_call('reset')
            .times(1 if from_latest is True else 0)
            .with_args('origin/the-branch'))

        (flexmock(build_orchestrate_build)
            .should_receive('override_build_kwarg')
            .times(1 if from_latest is True else 0)
            .with_args(workflow, 'git_ref', 'HEAD-OF-origin/the-branch'))

        workflow.source.config.autorebuild = dict(from_latest=from_latest)

        runner.run()
        assert workflow.prebuild_results[CheckAndSetRebuildPlugin.key] is True
        assert is_rebuild(workflow)
