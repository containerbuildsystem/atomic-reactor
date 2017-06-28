"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals
import pprint
import atomic_reactor.plugins.pre_check_and_set_rebuild
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.plugins.pre_cancel_builds_on_autorebuild import CancelBuildsOnAutoRebuild
from atomic_reactor.plugins.pre_check_and_set_rebuild import CheckAndSetRebuildPlugin
from atomic_reactor.util import ImageName
import json
from osbs.api import OSBS
from flexmock import flexmock
from tests.constants import SOURCE, MOCK
if MOCK:
    from tests.docker_mock import mock_docker


class X(object):
    pass


class TestCancelBuildsOnAutoRebuild(object):
    prebuild_plugins = [{
        'name': CancelBuildsOnAutoRebuild.key,
        'args': {
            "url": "",
        }
    }]

    test_build_name = "testbuild-1"

    test_build_list = [
        "testbuild-1",
        "testbuild-2",
        "testbuild-3"
    ]

    def assert_message_logged(self, msg, cplog):
        assert any([msg in l.getMessage() for l in cplog.records()])

    def prepare(self):
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
        setattr(workflow, 'prebuild_results', {})

        runner = PreBuildPluginsRunner(tasker, workflow, [
            {
                'name': CancelBuildsOnAutoRebuild.key,
                'args': {
                    'url': ''
                }
            }
        ])
        return workflow, runner

    def test_without_rebuild(self, monkeypatch, caplog):
        key = 'rebuild'
        value = 'false'
        buildconfig = "buildconfig1"

        workflow, runner = self.prepare()
        workflow.prebuild_results[CheckAndSetRebuildPlugin.key] = False

        build_json = {
            "metadata": {
                "labels": {
                    "buildconfig": buildconfig,
                    key: value,
                }
            }
        }

        monkeypatch.setenv("BUILD", json.dumps(build_json))
        runner.run()
        self.assert_message_logged(
            "this is not an autorebuild, %s is doing nothing" % CancelBuildsOnAutoRebuild.key,
            caplog
        )

    def test_with_rebuild(self, monkeypatch, caplog):
        key = 'rebuild'
        value = 'true'
        workflow, runner = self.prepare()
        workflow.prebuild_results[CheckAndSetRebuildPlugin.key] = True

        monkeypatch.setenv("BUILD", json.dumps({
            "metadata": {
                "labels": {
                    "buildconfig": "buildconfig1",
                    key: value,
                }
            }
        }))

        atomic_reactor.plugins.pre_check_and_set_rebuild = flexmock(
            is_rebuild=lambda x: True
        )

        flexmock(OSBS)
        OSBS.should_receive("list_builds").and_return(
            [
                flexmock(
                    get_build_name=lambda: build,
                    is_running=lambda: True,
                    build_id=build,
                )
                for build in self.test_build_list
            ]
        )

        for build in self.test_build_list:
            OSBS.should_receive("cancel_build").with_args(build)

        runner.run()
        pprint.pprint(caplog.records(), indent=2)
        for build in self.test_build_list:
            self.assert_message_logged(
                "cancelling build %s in favor of autorebuild" % build,
                caplog
            )

    def test_with_list_of_rebuilds(self, monkeypatch, caplog):
        key = 'rebuild'
        value = 'true'
        workflow, runner = self.prepare()
        workflow.prebuild_results[CheckAndSetRebuildPlugin.key] = True

        monkeypatch.setenv("BUILD", json.dumps({
            "metadata": {
                "labels": {
                    "buildconfig": "buildconfig1",
                    key: value,
                }
            }
        }))

        atomic_reactor.plugins.pre_check_and_set_rebuild = flexmock(
            is_rebuild=lambda x: True
        )

        flexmock(OSBS)
        OSBS.should_receive("list_builds").and_return(
            [
                flexmock(
                    get_build_name=lambda: self.test_build_name,
                    is_running=lambda: True,
                    build_id=self.test_build_name
                )
            ]
        )
        OSBS.should_receive("cancel_build").with_args(self.test_build_name)

        runner.run()
        pprint.pprint(caplog.records(), indent=2)
        self.assert_message_logged(
            "cancelling build %s in favor of autorebuild" % self.test_build_name,
            caplog
        )
