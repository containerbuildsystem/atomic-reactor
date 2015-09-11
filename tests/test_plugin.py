"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import json
import os

from flexmock import flexmock
import pytest

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PostBuildPluginsRunner, \
        InputPluginsRunner, PluginFailedException
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin
from atomic_reactor.plugins.pre_add_yum_repo_by_url import AddYumRepoByUrlPlugin
from atomic_reactor.util import ImageName
from tests.fixtures import docker_tasker
from tests.constants import DOCKERFILE_GIT, MOCK
if MOCK:
    from tests.docker_mock import mock_docker


TEST_IMAGE = "fedora:latest"
SOURCE = {"provider": "git", "uri": DOCKERFILE_GIT}


def test_load_prebuild_plugins(docker_tasker):
    runner = PreBuildPluginsRunner(docker_tasker, DockerBuildWorkflow(SOURCE, ""), None)
    assert runner.plugin_classes is not None
    assert len(runner.plugin_classes) > 0


def test_load_postbuild_plugins(docker_tasker):
    runner = PostBuildPluginsRunner(docker_tasker, DockerBuildWorkflow(SOURCE, ""), None)
    assert runner.plugin_classes is not None
    assert len(runner.plugin_classes) > 0


class X(object):
    pass


def test_build_plugin_failure(docker_tasker):
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    assert workflow.build_process_failed is False
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'base_image', ImageName(repo='fedora', tag='21'))
    setattr(workflow.builder, "source", X())
    setattr(workflow.builder.source, 'dockerfile_path', "/non/existent")
    setattr(workflow.builder.source, 'path', "/non/existent")
    runner = PreBuildPluginsRunner(docker_tasker, workflow,
                                   [{"name": AddYumRepoByUrlPlugin.key,
                                     "args": {'repourls': True}}])
    with pytest.raises(PluginFailedException):
        results = runner.run()
    assert workflow.build_process_failed is True


class TestInputPluginsRunner(object):
    def test_substitution(self, tmpdir):
        tmpdir_path = str(tmpdir)
        build_json_path = os.path.join(tmpdir_path, "build.json")
        with open(build_json_path, 'w') as fp:
            json.dump({
                "image": "some-image"
            }, fp)
        changed_image_name = "changed-image-name"
        runner = InputPluginsRunner([{"name": "path",
                                      "args": {
                                          "path": build_json_path,
                                          "substitutions": {
                                              "image": changed_image_name
        }}}])
        results = runner.run()
        assert results['path']['image'] == changed_image_name


    def test_substitution_on_plugins(self, tmpdir):
        tmpdir_path = str(tmpdir)
        build_json_path = os.path.join(tmpdir_path, "build.json")
        with open(build_json_path, 'w') as fp:
            json.dump({
                "image": "some-image",
                "prebuild_plugins": [{
                    'name': 'asd',
                    'args': {
                        'key': 'value1'
                    }
                }]
            }, fp)
        changed_value = "value-123"
        runner = InputPluginsRunner([{"name": "path",
                                      "args": {"path": build_json_path,
                                               "substitutions": {
                                                   "prebuild_plugins.asd.key": changed_value}}}])
        results = runner.run()
        assert results['path']['prebuild_plugins'][0]['args']['key'] == changed_value

    def test_autoinput_no_autousable(self):
        flexmock(os, environ={})
        runner = InputPluginsRunner([{'name': 'auto', 'args': {}}])
        with pytest.raises(PluginFailedException) as e:
            runner.run()
        assert 'No autousable input plugin' in str(e)

    def test_autoinput_more_autousable(self):
        # mock env vars checked by both env and osv3 input plugins
        flexmock(os, environ={'BUILD': 'a', 'SOURCE_URI': 'b', 'OUTPUT_IMAGE': 'c', 'BUILD_JSON': 'd'})
        runner = InputPluginsRunner([{'name': 'auto', 'args': {}}])
        with pytest.raises(PluginFailedException) as e:
            runner.run()
        assert 'More than one usable plugin with "auto" input' in str(e)
        assert 'osv3, env' in str(e) or 'env, osv3' in str(e)

    def test_autoinput_one_autousable(self):
        # mock env var for env input plugin
        flexmock(os, environ={'BUILD_JSON': json.dumps({'image': 'some-image'})})
        runner = InputPluginsRunner([{'name': 'auto', 'args': {'substitutions': {}}}])
        results = runner.run()
        assert results == {'auto': {'image': 'some-image'}}
