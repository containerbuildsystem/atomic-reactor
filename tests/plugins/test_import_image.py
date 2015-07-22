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

    # No-op implementation until this is implemented in osbs-client
    setattr(OSBS, 'import_image', lambda **kwargs: None)

    flexmock(OSBS, import_image=lambda name: None)

    runner = PostBuildPluginsRunner(tasker, workflow, [{
        'name': ImportImagePlugin.key,
        'args': {
            'url': '',
            'verify_ssl': False,
            'use_auth': False
        }}])

    return runner


def must_not_be_called(*_):
    """
    Set as implementation for methods than must not be called
    """

    assert False


def test_bad_setup():
    """
    Try all the early-fail paths.
    """

    runner = prepare()

    flexmock(OSBS, import_image=must_not_be_called)

    # No build JSON
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

    def raise_exc(self, *args, **kwargs):
        raise RuntimeError        


def test_import_image():
    """
    Test action of plugin.
    """

    runner = prepare()

    my_imagestream = 'fedora'

    collect = Collect()
    flexmock(OSBS, import_image=collect.called)

    # No namespace in metadata
    os.environ["BUILD"] = json.dumps({
        "metadata": {
            "labels": {
                "imagestream": my_imagestream
            }
        }
    })
    runner.run()

    # import_image() is called with the correct arguments
    # (no namespace keyword)
    assert collect.called_with == [((my_imagestream,), {})]

    # Namespace in metadata
    collect = Collect()
    flexmock(OSBS, import_image=collect.called)
    namespace = 'namespace'
    os.environ["BUILD"] = json.dumps({
        "metadata": {
            "namespace": namespace,
            "labels": {
                "imagestream": my_imagestream
            }
        }
    })
    runner.run()

    # import_image() is called with the correct arguments
    # (including namespace keyword)
    assert collect.called_with == [((my_imagestream,),
                                    {'namespace': namespace})]


def test_exception_during_import():
    """
    The plugin should fail if the import fails.
    """

    runner = prepare()

    my_imagestream = 'fedora'
    collect = Collect()
    flexmock(OSBS, import_image=collect.raise_exc)

    os.environ["BUILD"] = json.dumps({
        "metadata": {
            "labels": {
                "imagestream": my_imagestream
            }
        }
    })

    with pytest.raises(PluginFailedException):
        runner.run()
