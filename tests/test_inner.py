"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from atomic_reactor.build import InsideBuilder
from atomic_reactor.util import ImageName
from atomic_reactor.plugin import PreBuildPlugin, PrePublishPlugin, PostBuildPlugin, ExitPlugin
from atomic_reactor.plugin import PluginFailedException
from flexmock import flexmock
import pytest
from tests.constants import MOCK_SOURCE
from tests.docker_mock import mock_docker
import inspect

from atomic_reactor.inner import DockerBuildWorkflow


class MockDockerTasker(object):
    def inspect_image(self, name):
        return {}


class X(object):
    pass


class MockInsideBuilder(object):
    def __init__(self):
        self.tasker = MockDockerTasker()
        self.base_image = ImageName(repo='Fedora', tag='22')
        self.image_id = 'asd'

    @property
    def source(self):
        result = X()
        setattr(result, 'dockerfile_path', '/')
        setattr(result, 'path', '/tmp')
        return result

    def pull_base_image(self, source_registry, insecure=False):
        pass

    def build(self):
        result = X()
        setattr(result, 'logs', None)
        setattr(result, 'is_failed', lambda: False)
        return result

    def inspect_built_image(self):
        return None


class PreRaises(PreBuildPlugin):
    """
    This plugin must run and cause the build to abort.
    """

    can_fail = False
    key = 'pre_raises'

    def run(self):
        raise RuntimeError


class PreWatched(PreBuildPlugin):
    """
    A PreBuild plugin we can watch.
    """

    key = 'pre_watched'

    def __init__(self, tasker, workflow, watcher, *args, **kwargs):
        super(PreWatched, self).__init__(tasker, workflow, *args, **kwargs)
        self.watcher = watcher

    def run(self):
        self.watcher.call()


class PrePubWatched(PrePublishPlugin):
    """
    A PrePublish plugin we can watch.
    """

    key = 'prepub_watched'

    def __init__(self, tasker, workflow, watcher, *args, **kwargs):
        super(PrePubWatched, self).__init__(tasker, workflow, *args, **kwargs)
        self.watcher = watcher

    def run(self):
        self.watcher.call()


class PostWatched(PostBuildPlugin):
    """
    A PostBuild plugin we can watch.
    """

    key = 'post_watched'

    def __init__(self, tasker, workflow, watcher, *args, **kwargs):
        super(PostWatched, self).__init__(tasker, workflow, *args, **kwargs)
        self.watcher = watcher

    def run(self):
        self.watcher.call()


class ExitWatched(ExitPlugin):
    """
    An Exit plugin we can watch.
    """

    key = 'exit_watched'

    def __init__(self, tasker, workflow, watcher, *args, **kwargs):
        super(ExitWatched, self).__init__(tasker, workflow, *args, **kwargs)
        self.watcher = watcher

    def run(self):
        self.watcher.call()


class ExitCompat(ExitPlugin):
    """
    An Exit plugin called as a Post-build plugin.
    """

    key = 'store_logs_to_file'

    def __init__(self, tasker, workflow, watcher, *args, **kwargs):
        super(ExitCompat, self).__init__(tasker, workflow, *args, **kwargs)
        self.watcher = watcher

    def run(self):
        self.watcher.call()


class Watcher(object):
    def __init__(self):
        self.called = False

    def call(self):
        self.called = True

    def was_called(self):
        return self.called


def test_workflow():
    """
    Test normal workflow.
    """

    this_file = inspect.getfile(PreWatched)
    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)
    watch_pre = Watcher()
    watch_prepub = Watcher()
    watch_post = Watcher()
    watch_exit = Watcher()
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image',
                                   prebuild_plugins=[{'name': 'pre_watched',
                                                      'args': {
                                                          'watcher': watch_pre
                                                      }}],
                                   postbuild_plugins=[{'name': 'post_watched',
                                                       'args': {
                                                           'watcher': watch_post
                                                       }}],
                                   prepublish_plugins=[{'name': 'prepub_watched',
                                                        'args': {
                                                            'watcher': watch_prepub,
                                                        }}],
                                   exit_plugins=[{'name': 'exit_watched',
                                                  'args': {
                                                      'watcher': watch_exit
                                                  }}],
                                   plugin_files=[this_file])

    workflow.build_docker_image()

    assert watch_pre.was_called()
    assert watch_prepub.was_called()
    assert watch_post.was_called()
    assert watch_exit.was_called()


def test_workflow_compat():
    """
    Some of our plugins have changed from being run post-build to
    being run at exit. Let's test what happens when we try running an
    exit plugin as a post-build plugin.
    """

    this_file = inspect.getfile(PreWatched)
    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)
    watch_exit = Watcher()
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image',
                                   postbuild_plugins=[{'name': 'store_logs_to_file',
                                                       'args': {
                                                           'watcher': watch_exit
                                                       }}],
                                   plugin_files=[this_file])

    workflow.build_docker_image()
    assert watch_exit.was_called()


def test_workflow_errors():
    """
    This is a test for what happens when plugins fail.

    When a prebuild plugin fails, no postbuild plugins should run.
    However, all the exit plugins should run.
    """

    this_file = inspect.getfile(PreRaises)
    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)
    watch_pre = Watcher()
    watch_post = Watcher()
    watch_exit = Watcher()
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image',
                                   prebuild_plugins=[{'name': 'pre_raises',
                                                      'args': {}},
                                                     {'name': 'pre_watched',
                                                      'args': {
                                                          'watcher': watch_pre
                                                      }}],
                                   postbuild_plugins=[{'name': 'post_watched',
                                                       'args': {
                                                           'watcher': watch_post
                                                       }}],
                                   exit_plugins=[{'name': 'exit_watched',
                                                  'args': {
                                                      'watcher': watch_exit
                                                  }}],
                                   plugin_files=[this_file])

    with pytest.raises(PluginFailedException):
        workflow.build_docker_image()

    assert not watch_post.was_called()
    assert watch_exit.was_called()

    # What about plugins in the same class, e.g. prebuild plugins,
    # that are listed after a plugin that failed?
    # Currently, they *do* run.
    #assert not watch_pre.was_called()
