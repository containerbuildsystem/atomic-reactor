"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import json
import os

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import ExitPluginsRunner, PluginFailedException
from atomic_reactor.util import ImageName
from tests.constants import INPUT_IMAGE, SOURCE
from atomic_reactor.plugins.exit_remove_buildconfig import RemoveBuildConfigPlugin

from osbs.api import OSBS
from flexmock import flexmock
import pytest


class X(object):
    image_id = INPUT_IMAGE
    git_dockerfile_path = None
    git_path = None
    base_image = ImageName(repo="qwe", tag="asd")


def prepare(build_process_failed=False):
    """
    Boiler-plate test set-up
    """

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'image_id', 'asd123')
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)
    setattr(workflow, 'plugin_failed', build_process_failed)

    flexmock(OSBS)

    # No-op implementation until implemented in osbs-client
    setattr(OSBS, 'delete_buildconfig', lambda **kwargs: None)

    flexmock(OSBS, delete_buildconfig=lambda name: None)

    runner = ExitPluginsRunner(tasker, workflow, [{
        'name': RemoveBuildConfigPlugin.key,
        'args': {
            'url': '',
            'verify_ssl': False,
            'use_auth': False
        }}])

    return runner


def must_not_be_called(*_, **__):
    """
    Set as implementation for methods than must not be called
    """

    assert False


def test_bad_setup():
    """
    Try all the early-fail paths.
    """

    runner = prepare(build_process_failed=True)

    flexmock(OSBS, delete_buildconfig=must_not_be_called)

    # No build JSON
    with pytest.raises(PluginFailedException):
        runner.run()

    # No metadata
    os.environ["BUILD"] = json.dumps({})
    with pytest.raises(PluginFailedException):
        runner.run()


class Collect(object):
    """
    Collect the values a method is called with.
    """

    def __init__(self):
        self.called_with = []

    def called(self, *args, **kwargs):
        """
        Set this as the implementation for the method to watch.
        """
        self.called_with.append((args, kwargs))


def test_failed():
    """
    Test what happens when Build failed.
    """

    runner = prepare(build_process_failed=True)

    my_buildconfig_id = 'my-buildconfig-id'
    my_namespace = 'namespace'

    collect = Collect()
    flexmock(OSBS, delete_buildconfig=collect.called)

    os.environ["BUILD"] = json.dumps({
        "metadata": {
            "namespace": my_namespace,
            "labels": {
                "buildconfig": my_buildconfig_id
            }
        }
    })

    # Build failing
    runner.run()

    # Our own BuildConfig is deleted
    assert collect.called_with == [((my_buildconfig_id,),
                                    {'namespace': my_namespace})]


def test_succeeded():
    """
    Test what happens when the Build succeeded, i.e. a normal run.
    """

    runner = prepare(build_process_failed=False)

    flexmock(OSBS, delete_buildconfig=must_not_be_called)

    # Build succeeding
    os.environ["BUILD"] = json.dumps({
        "metadata": {
            "namespace": "default",
            "labels": {
                "buildconfig": "my-buildconfig-id",
            }
        }
    })
    runner.run()
