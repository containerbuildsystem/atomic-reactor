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
from atomic_reactor.plugins.pre_check_rebuild import CheckRebuildPlugin, is_rebuild
from atomic_reactor.util import ImageName
from tests.constants import SOURCE
import json
import os


class X(object):
    pass


def prepare(key, value):
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'image_id', 'asd123')
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='22'))
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder.source, 'path', '/tmp')
    setattr(workflow.builder.source, 'dockerfile_path', None)
    runner = PreBuildPluginsRunner(tasker, workflow,
                                   [
                                       {
                                           'name': CheckRebuildPlugin.key,
                                           'args': {
                                               'key': key,
                                               'value': value,
                                           },
                                       }
                                   ])
    return workflow, runner


def test_check_rebuild_no_build_json():
    workflow, runner = prepare('client', 'osbs')
    if "BUILD" in os.environ:
        del os.environ["BUILD"]

    with pytest.raises(PluginFailedException):
        runner.run()


def test_check_is_not_rebuild():
    key = 'client'
    value = 'osbs'
    workflow, runner = prepare(key, value)

    os.environ["BUILD"] = json.dumps({
        "metadata": {
            key: value,
        }
    })

    runner.run()
    assert workflow.prebuild_results[CheckRebuildPlugin.key] == False
    assert not is_rebuild(workflow)


@pytest.mark.parametrize('build_json', [
    {},

    {
        "metadata": {},
    },

    {
        "metadata": {
            "client": "mismatch",
        }
    }
])
def test_check_is_rebuild(build_json):
    key = 'client'
    value = 'osbs'
    workflow, runner = prepare(key, value)

    os.environ["BUILD"] = json.dumps(build_json)

    runner.run()
    assert workflow.prebuild_results[CheckRebuildPlugin.key] == True
    assert is_rebuild(workflow)
