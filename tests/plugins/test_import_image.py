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
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from atomic_reactor.util import ImageName
from tests.constants import INPUT_IMAGE, SOURCE
from atomic_reactor.plugins.post_import_image import ImportImagePlugin

from osbs.api import OSBS
from flexmock import flexmock
import pytest


class X(object):
    image_id = INPUT_IMAGE
    git_dockerfile_path = None
    git_path = None
    base_image = ImageName(repo="qwe", tag="asd")


def prepare():
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
    flexmock(OSBS)

    runner = PostBuildPluginsRunner(tasker, workflow, [{
        'name': ImportImagePlugin.key,
        'args': {
            'url': '',
            'verify_ssl': False,
            'use_auth': False
        }}])

    return runner


def test_bad_setup():
    """
    Try all the early-fail paths.
    """

    runner = prepare()

    (flexmock(OSBS)
     .should_receive('import_image')
     .never())

    # No build JSON
    if "BUILD" in os.environ:
        del os.environ["BUILD"]
    with pytest.raises(PluginFailedException):
        runner.run()

    # No metadata
    os.environ["BUILD"] = json.dumps({})
    with pytest.raises(PluginFailedException):
        runner.run()

    # No imagestream label
    os.environ["BUILD"] = json.dumps({
        "metadata": {
            "labels": {
            }
        }
    })
    with pytest.raises(PluginFailedException):
        runner.run()


def test_import_image():
    """
    Test action of plugin.
    """

    runner = prepare()

    my_imagestream = 'fedora'

    # Check import_image() is called with the correct arguments
    # (no namespace keyword)
    (flexmock(OSBS)
     .should_receive('import_image')
     .with_args(my_imagestream))

    # No namespace in metadata
    os.environ["BUILD"] = json.dumps({
        "metadata": {
            "labels": {
                "imagestream": my_imagestream
            }
        }
    })
    runner.run()

    # Namespace in metadata
    namespace = 'namespace'

    # Check import_image() is called with the correct arguments
    # (including namespace keyword)
    (flexmock(OSBS)
     .should_receive('import_image')
     .with_args(my_imagestream, namespace=namespace))

    os.environ["BUILD"] = json.dumps({
        "metadata": {
            "namespace": namespace,
            "labels": {
                "imagestream": my_imagestream
            }
        }
    })
    runner.run()


def test_exception_during_import():
    """
    The plugin should fail if the import fails.
    """

    runner = prepare()

    my_imagestream = 'fedora'
    (flexmock(OSBS)
     .should_receive('import_image')
     .and_raise(RuntimeError))

    os.environ["BUILD"] = json.dumps({
        "metadata": {
            "labels": {
                "imagestream": my_imagestream
            }
        }
    })

    with pytest.raises(PluginFailedException):
        runner.run()
