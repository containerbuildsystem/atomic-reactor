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
from atomic_reactor.util import ImageName
import json
from osbs.api import OSBS
import osbs.conf
from osbs.exceptions import OsbsResponseException
from flexmock import flexmock
from tests.constants import SOURCE, MOCK
if MOCK:
    from tests.docker_mock import mock_docker


class X(object):
    pass


class TestCheckRebuild(object):
    def prepare(self, key, value, set_labels_args=None, set_labels_kwargs=None):
        if MOCK:
            mock_docker()
        tasker = DockerTasker()
        workflow = DockerBuildWorkflow(SOURCE, "test-image")
        setattr(workflow, 'builder', X())
        setattr(workflow.builder, 'image_id', 'asd123')
        setattr(workflow.builder, 'base_image', ImageName(repo='Fedora',
                                                          tag='22'))
        setattr(workflow.builder, 'source', X())
        setattr(workflow.builder.source, 'path', '/tmp')
        setattr(workflow.builder.source, 'dockerfile_path', None)
        expectation = (flexmock(OSBS)
                       .should_receive('set_labels_on_build_config'))
        if set_labels_args is not None:
            if set_labels_kwargs is None:
                set_labels_kwargs = {}

            expectation.with_args(*set_labels_args)

        namespace = None
        if set_labels_kwargs is not None:
            namespace = set_labels_kwargs.get('namespace')
        (flexmock(osbs.conf).should_call('Configuration')
         .with_args(namespace=namespace, conf_file=None, verify_ssl=True, openshift_url="",
                    openshift_uri="", use_auth=True))

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

    def test_check_rebuild_no_build_json(self, monkeypatch):
        workflow, runner = self.prepare('is_autorebuild', 'true')
        monkeypatch.delenv('BUILD', raising=False)

        with pytest.raises(PluginFailedException):
            runner.run()

    def test_check_no_buildconfig(self, monkeypatch):
        key = 'is_autorebuild'
        value = 'true'
        workflow, runner = self.prepare(key, value)
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
    def test_check_is_not_rebuild(self, namespace, monkeypatch):
        key = 'is_autorebuild'
        value = 'true'
        buildconfig = "buildconfig1"
        namespace_dict = {}
        if namespace is not None:
            namespace_dict["namespace"] = namespace

        workflow, runner = self.prepare(key, value,
                                        set_labels_args=(buildconfig,
                                                         {key: value}),
                                        set_labels_kwargs=namespace_dict)

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
        assert workflow.prebuild_results[CheckAndSetRebuildPlugin.key] == False
        assert not is_rebuild(workflow)

    def test_check_is_rebuild(self, monkeypatch):
        key = 'is_autorebuild'
        value = 'true'
        workflow, runner = self.prepare(key, value)

        monkeypatch.setenv("BUILD", json.dumps({
            "metadata": {
                "labels": {
                    "buildconfig": "buildconfig1",
                    key: value,
                }
            }
        }))
        runner.run()
        assert workflow.prebuild_results[CheckAndSetRebuildPlugin.key] == True
        assert is_rebuild(workflow)

    def test_409_response(self, monkeypatch):
        key = 'is_autorebuild'
        workflow, runner = self.prepare(key, 'true')

        (flexmock(OSBS)
            .should_receive('set_labels_on_build_config')
            .twice()
            .and_raise(OsbsResponseException('conflict', 409))
            .and_return(None))

        monkeypatch.setenv("BUILD", json.dumps({
            "metadata": {
                "labels": {
                    "buildconfig": "buildconfig1",
                    key: 'false',
                }
            }
        }))
        runner.run()
