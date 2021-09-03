"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import pytest
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_check_and_set_rebuild import (is_rebuild,
                                                              CheckAndSetRebuildPlugin)
from atomic_reactor.plugins import build_orchestrate_build
from atomic_reactor.util import DockerfileImages
import json
from osbs.api import OSBS
import osbs.conf
from flexmock import flexmock
from tests.constants import MOCK
if MOCK:
    from tests.docker_mock import mock_docker


class MockInsideBuilder(object):
    def __init__(self, tmpdir):
        self.tasker = DockerTasker()
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
                base_from_scratch=False, custom_base=False):
        if MOCK:
            mock_docker()
        tasker = DockerTasker()

        workflow = DockerBuildWorkflow(source=None)
        parent_images = None
        if base_from_scratch:
            parent_images = ['scratch']
        if custom_base:
            parent_images = ['koji/image-build']

        if parent_images:
            workflow.builder = MockInsideBuilder(tmpdir)
            workflow.dockerfile_images = DockerfileImages(parent_images)
        else:
            workflow.dockerfile_images = DockerfileImages([])
            workflow.builder = MockInsideBuilder(tmpdir)

        workflow.source = workflow.builder.source
        setattr(workflow.builder, 'base_image_inspect', {})

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

        rcm = {'version': 1, 'openshift': {'url': '', 'insecure': False, 'auth': {'enable': True}}}
        workflow.conf.conf = rcm

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

    def test_check_rebuild_no_build_json(self, tmpdir, monkeypatch, user_params):
        _, runner = self.prepare(tmpdir, 'is_autorebuild', 'true')
        monkeypatch.delenv('BUILD', raising=False)

        with pytest.raises(PluginFailedException):
            runner.run()

    def test_check_no_buildconfig(self, tmpdir, monkeypatch, user_params):
        key = 'is_autorebuild'
        value = 'true'
        _, runner = self.prepare(tmpdir, key, value)
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
    def test_check_is_not_rebuild(self, tmpdir, namespace, monkeypatch, user_params):
        key = 'is_autorebuild'
        value = 'true'
        buildconfig = "buildconfig1"
        namespace_dict = {}
        if namespace is not None:
            namespace_dict["namespace"] = namespace

        workflow, runner = self.prepare(tmpdir, key, value,
                                        update_labels_args=(buildconfig,
                                                            {key: value}),
                                        update_labels_kwargs=namespace_dict)
        workflow.user_params['namespace'] = namespace

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

    @pytest.mark.parametrize(('custom_base', 'base_from_scratch'), [
        (True, False),
        (False, True),
        (False, False),
    ])
    @pytest.mark.parametrize('from_latest', (None, True, False))
    def test_check_is_rebuild(self, caplog, tmpdir, monkeypatch, user_params,
                              base_from_scratch, custom_base, from_latest):
        key = 'is_autorebuild'
        value = 'true'

        workflow, runner = self.prepare(tmpdir, key, value,
                                        base_from_scratch=base_from_scratch,
                                        custom_base=custom_base)

        monkeypatch.setenv("BUILD", json.dumps({
            "metadata": {
                "labels": {
                    "buildconfig": "buildconfig1",
                    key: value,
                    'git-branch': 'the-branch',
                }
            }
        }))

        mock_times = 1 if from_latest and not (base_from_scratch or custom_base) else 0
        (flexmock(workflow.source)
            .should_call('reset')
            .times(mock_times)
            .with_args('origin/the-branch'))

        (flexmock(build_orchestrate_build)
            .should_receive('override_build_kwarg')
            .times(mock_times)
            .with_args(workflow, 'git_ref', 'HEAD-OF-origin/the-branch'))

        workflow.source.config.autorebuild = dict(from_latest=from_latest)

        runner.run()
        if base_from_scratch:
            assert workflow.prebuild_results[CheckAndSetRebuildPlugin.key] is False
            assert not is_rebuild(workflow)
            log_msg = "Skipping check and set rebuild: unsupported for FROM-scratch images"
            assert log_msg in caplog.text
        elif custom_base:
            assert workflow.prebuild_results[CheckAndSetRebuildPlugin.key] is False
            assert not is_rebuild(workflow)
            log_msg = "Skipping check and set rebuild: unsupported for custom base images"
            assert log_msg in caplog.text
        else:
            assert workflow.prebuild_results[CheckAndSetRebuildPlugin.key] is True
            assert is_rebuild(workflow)

    @pytest.mark.parametrize(('scratch', 'isolated'), [
        (True, False),
        (True, True),
        (False, True),
    ])
    def test_skip_build(self, tmpdir, caplog, user_params, scratch, isolated):
        workflow, runner = self.prepare(tmpdir, 'is_autorebuild', 'true')
        workflow.user_params['scratch'] = scratch
        workflow.user_params['isolated'] = isolated

        runner.run()

        if scratch:
            log_msg = 'scratch build, skipping plugin'
        elif isolated:
            log_msg = 'isolated build, skipping plugin'

        assert log_msg in caplog.text
