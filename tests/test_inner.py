"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import json
import os

from atomic_reactor.build import InsideBuilder
from atomic_reactor.util import ImageName
from atomic_reactor.plugin import (PreBuildPlugin, PrePublishPlugin, PostBuildPlugin, ExitPlugin,
                                   AutoRebuildCanceledException)
from atomic_reactor.plugin import PluginFailedException
import atomic_reactor.plugin
import logging
from flexmock import flexmock
import pytest
from tests.constants import MOCK_SOURCE, SOURCE
from tests.docker_mock import mock_docker
from tests.util import requires_internet
import inspect

from atomic_reactor.inner import BuildResults, BuildResultsEncoder, BuildResultsJSONDecoder
from atomic_reactor.inner import DockerBuildWorkflow


BUILD_RESULTS_ATTRS = ['build_logs',
                       'built_img_inspect',
                       'built_img_info',
                       'base_img_info',
                       'base_plugins_output',
                       'built_img_plugins_output']


def test_build_results_encoder():
    results = BuildResults()
    expected_data = {}
    for attr in BUILD_RESULTS_ATTRS:
        setattr(results, attr, attr)
        expected_data[attr] = attr

    data = json.loads(json.dumps(results, cls=BuildResultsEncoder))
    assert data == expected_data


def test_build_results_decoder():
    data = {}
    expected_results = BuildResults()
    for attr in BUILD_RESULTS_ATTRS:
        setattr(expected_results, attr, attr)
        data[attr] = attr

    results = json.loads(json.dumps(data), cls=BuildResultsJSONDecoder)
    for attr in set(BUILD_RESULTS_ATTRS) - set(['build_logs']):
        assert getattr(results, attr) == getattr(expected_results, attr)


class MockDockerTasker(object):
    def inspect_image(self, name):
        return {}


class X(object):
    pass


class MockInsideBuilder(object):
    def __init__(self, failed=False):
        self.tasker = MockDockerTasker()
        self.base_image = ImageName(repo='Fedora', tag='22')
        self.image_id = 'asd'
        self.failed = failed

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
        setattr(result, 'is_failed', lambda: self.failed)
        return result

    def inspect_built_image(self):
        return None


class RaisesMixIn(object):
    """
    Mix-in class for plugins that should raise exceptions.
    """

    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, *args, **kwargs):
        super(RaisesMixIn, self).__init__(tasker, workflow,
                                          *args, **kwargs)

    def run(self):
        raise RuntimeError


class PreRaises(RaisesMixIn, PreBuildPlugin):
    """
    This plugin must run and cause the build to abort.
    """

    key = 'pre_raises'


class PostRaises(RaisesMixIn, PostBuildPlugin):
    """
    This plugin must run and cause the build to abort.
    """

    key = 'post_raises'


class PrePubRaises(RaisesMixIn, PrePublishPlugin):
    """
    This plugin must run and cause the build to abort.
    """

    key = 'prepub_raises'


class WatchedMixIn(object):
    """
    Mix-in class for plugins we want to watch.
    """

    def __init__(self, tasker, workflow, watcher, *args, **kwargs):
        super(WatchedMixIn, self).__init__(tasker, workflow,
                                           *args, **kwargs)
        self.watcher = watcher

    def run(self):
        self.watcher.call()


class PreWatched(WatchedMixIn, PreBuildPlugin):
    """
    A PreBuild plugin we can watch.
    """

    key = 'pre_watched'


class PrePubWatched(WatchedMixIn, PrePublishPlugin):
    """
    A PrePublish plugin we can watch.
    """

    key = 'prepub_watched'


class PostWatched(WatchedMixIn, PostBuildPlugin):
    """
    A PostBuild plugin we can watch.
    """

    key = 'post_watched'


class ExitWatched(WatchedMixIn, ExitPlugin):
    """
    An Exit plugin we can watch.
    """

    key = 'exit_watched'


class ExitRaises(RaisesMixIn, ExitPlugin):
    """
    An Exit plugin that should raise an exception.
    """

    key = 'exit_raises'


class ExitRaisesAllowed(RaisesMixIn, ExitPlugin):
    """
    An Exit plugin that should raise an exception.
    """

    is_allowed_to_fail = True

    key = 'exit_raises_allowed'


class ExitCompat(WatchedMixIn, ExitPlugin):
    """
    An Exit plugin called as a Post-build plugin.
    """

    key = 'store_logs_to_file'


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
                                   prepublish_plugins=[{'name': 'prepub_watched',
                                                        'args': {
                                                            'watcher': watch_prepub,
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

    workflow.build_docker_image()

    assert watch_pre.was_called()
    assert watch_prepub.was_called()
    assert watch_post.was_called()
    assert watch_exit.was_called()


class FakeLogger(object):
    def __init__(self):
        self.debugs = []
        self.infos = []
        self.warnings = []
        self.errors = []

    def log(self, logs, args):
        logs.append(args)

    def debug(self, *args):
        self.log(self.debugs, args)

    def info(self, *args):
        self.log(self.infos, args)

    def warning(self, *args):
        self.log(self.warnings, args)

    def error(self, *args):
        self.log(self.errors, args)


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
    fake_logger = FakeLogger()
    atomic_reactor.plugin.logger = fake_logger
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image',
                                   postbuild_plugins=[{'name': 'store_logs_to_file',
                                                       'args': {
                                                           'watcher': watch_exit
                                                       }}],
                                   plugin_files=[this_file])

    workflow.build_docker_image()
    assert watch_exit.was_called()
    assert len(fake_logger.errors) > 0


class Pre(PreBuildPlugin):
    """
    This plugin does nothing. It's only used for configuration testing.
    """

    key = 'pre'


class Post(PostBuildPlugin):
    """
    This plugin does nothing. It's only used for configuration testing.
    """

    key = 'post'


class Exit(ExitPlugin):
    """
    This plugin does nothing. It's only used for configuration testing.
    """

    key = 'exit'


@pytest.mark.parametrize(('plugins', 'should_fail', 'should_log'), [
    # No 'name' key, prebuild
    ({
        'prebuild_plugins': [{'args': {}},
                             {'name': 'pre_watched',
                              'args': {
                                  'watcher': Watcher(),
                              }
                             }],
      },
     True,  # is fatal
     True,  # logs error
    ),

    # No 'name' key, postbuild
    ({
        'postbuild_plugins': [{'args': {}},
                              {'name': 'post_watched',
                               'args': {
                                   'watcher': Watcher(),
                               }
                              }],
      },
     True,  # is fatal
     True,  # logs error
    ),

    # No 'name' key, exit
    ({
        'exit_plugins': [{'args': {}},
                         {'name': 'exit_watched',
                          'args': {
                              'watcher': Watcher(),
                          }
                         }],
      },
     False,  # not fatal
     True,   # logs error
    ),

    # No 'args' key, prebuild
    ({'prebuild_plugins': [{'name': 'pre'},
                           {'name': 'pre_watched',
                            'args': {
                                'watcher': Watcher(),
                            }
                           }]},
     False,  # not fatal
     False,  # no error logged
    ),

    # No 'args' key, postbuild
    ({'postbuild_plugins': [{'name': 'post'},
                            {'name': 'post_watched',
                             'args': {
                                 'watcher': Watcher(),
                             }
                            }]},
     False,  # not fatal,
     False,  # no error logged
    ),

    # No 'args' key, exit
    ({'exit_plugins': [{'name': 'exit'},
                       {'name': 'exit_watched',
                        'args': {
                            'watcher': Watcher(),
                        }
                       }]},
     False,  # not fatal
     False,  # no error logged
    ),

    # No such plugin, prebuild
    ({'prebuild_plugins': [{'name': 'no plugin',
                            'args': {}},
                           {'name': 'pre_watched',
                            'args': {
                                'watcher': Watcher(),
                            }
                           }]},
     True,  # is fatal
     True,  # logs error
    ),

    # No such plugin, postbuild
    ({'postbuild_plugins': [{'name': 'no plugin',
                             'args': {}},
                            {'name': 'post_watched',
                             'args': {
                                 'watcher': Watcher(),
                             }
                            }]},
     True,  # is fatal
     True,  # logs error
    ),

    # No such plugin, exit
    ({'exit_plugins': [{'name': 'no plugin',
                        'args': {}},
                       {'name': 'exit_watched',
                        'args': {
                            'watcher': Watcher(),
                        }
                       }]},
     False,  # not fatal
     True,   # logs error
    ),
])
def test_plugin_errors(plugins, should_fail, should_log):
    """
    Try bad plugin configuration.
    """

    this_file = inspect.getfile(PreRaises)
    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)
    fake_logger = FakeLogger()
    atomic_reactor.plugin.logger = fake_logger

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image',
                                   plugin_files=[this_file],
                                   **plugins)

    # Find the 'watcher' parameter
    watchers = [conf.get('args', {}).get('watcher')
                for plugin in plugins.values()
                for conf in plugin]
    watcher = [x for x in watchers if x][0]

    if should_fail:
        with pytest.raises(PluginFailedException):
            workflow.build_docker_image()

        assert not watcher.was_called()
    else:
        workflow.build_docker_image()
        assert watcher.was_called()

    if should_log:
        assert len(fake_logger.errors) > 0
    else:
        assert len(fake_logger.errors) == 0


class StopAutorebuildPlugin(PreBuildPlugin):
    key = 'stopstopstop'

    def run(self):
        raise AutoRebuildCanceledException(self.key, 'message')


def test_autorebuild_stop_prevents_build():
    """
    test that a plugin that raises AutoRebuildCanceledException results in actually skipped build
    """
    this_file = inspect.getfile(PreWatched)
    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)
    watch_prepub = Watcher()
    watch_post = Watcher()
    watch_exit = Watcher()
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image',
                                   prebuild_plugins=[{'name': 'stopstopstop',
                                                      'args': {
                                                      }}],
                                   prepublish_plugins=[{'name': 'prepub_watched',
                                                        'args': {
                                                            'watcher': watch_prepub,
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

    with pytest.raises(AutoRebuildCanceledException):
        workflow.build_docker_image()

    assert not watch_prepub.was_called()
    assert not watch_post.was_called()
    assert watch_exit.was_called()
    assert workflow.autorebuild_canceled == True


@pytest.mark.parametrize('fail_at', ['pre', 'prepub', 'post', 'exit', 'exit_allowed'])
def test_workflow_plugin_error(fail_at):
    """
    This is a test for what happens when plugins fail.

    When a prebuild or postbuild plugin fails, and doesn't have
    is_allowed_to_fail=True set, the whole build should fail.
    However, all the exit plugins should run.
    """

    this_file = inspect.getfile(PreRaises)
    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)
    watch_pre = Watcher()
    watch_prepub = Watcher()
    watch_post = Watcher()
    watch_exit = Watcher()
    prebuild_plugins = [{'name': 'pre_watched',
                         'args': {
                             'watcher': watch_pre,
                         }}]
    prepublish_plugins = [{'name': 'prepub_watched',
                           'args': {
                               'watcher': watch_prepub,
                           }}]
    postbuild_plugins = [{'name': 'post_watched',
                          'args': {
                              'watcher': watch_post
                          }}]
    exit_plugins = [{'name': 'exit_watched',
                     'args': {
                         'watcher': watch_exit
                     }}]

    # Insert a failing plugin into one of the build phases
    if fail_at == 'pre':
        prebuild_plugins.insert(0, {'name': 'pre_raises', 'args': {}})
    elif fail_at == 'prepub':
        prepublish_plugins.insert(0, {'name': 'prepub_raises', 'args': {}})
    elif fail_at == 'post':
        postbuild_plugins.insert(0, {'name': 'post_raises', 'args': {}})
    elif fail_at == 'exit':
        exit_plugins.insert(0, {'name': 'exit_raises', 'args': {}})
    elif fail_at == 'exit_allowed':
        exit_plugins.insert(0, {'name': 'exit_raises_allowed', 'args': {}})
    else:
        # Typo in the parameter list?
        assert False

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image',
                                   prebuild_plugins=prebuild_plugins,
                                   prepublish_plugins=prepublish_plugins,
                                   postbuild_plugins=postbuild_plugins,
                                   exit_plugins=exit_plugins,
                                   plugin_files=[this_file])

    # Most failures cause the build process to abort. Unless, it's
    # an exit plugin that's explicitly allowed to fail.
    if fail_at == 'exit_allowed':
        build_result = workflow.build_docker_image()
        assert build_result and not build_result.is_failed()
    else:
        with pytest.raises(PluginFailedException):
            workflow.build_docker_image()

    # The pre-build phase should only complete if there were no
    # earlier plugin failures.
    assert watch_pre.was_called() == (fail_at != 'pre')

    # The prepublish phase should only complete if there were no
    # earlier plugin failures.
    assert watch_prepub.was_called() == (fail_at not in ('pre', 'prepub'))

    # The post-build phase should only complete if there were no
    # earlier plugin failures.
    assert watch_post.was_called() == (fail_at not in ('pre', 'prepub', 'post'))

    # But all exit plugins should run, even if one of them also raises
    # an exception.
    assert watch_exit.was_called()


def test_workflow_docker_build_error():
    """
    This is a test for what happens when the docker build fails.
    """

    this_file = inspect.getfile(PreRaises)
    mock_docker()
    fake_builder = MockInsideBuilder(failed=True)
    flexmock(InsideBuilder).new_instances(fake_builder)
    watch_prepub = Watcher()
    watch_post = Watcher()
    watch_exit = Watcher()

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image',
                                   prepublish_plugins=[{'name': 'prepub_watched',
                                                        'args': {
                                                            'watcher': watch_prepub,
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

    assert workflow.build_docker_image().is_failed()

    # No subsequent build phases should have run except 'exit'
    assert not watch_prepub.was_called()
    assert not watch_post.was_called()
    assert watch_exit.was_called()


class ExitUsesSource(ExitWatched):
    key = 'uses_source'

    def run(self):
        assert os.path.exists(self.workflow.source.get_dockerfile_path()[0])
        WatchedMixIn.run(self)


@requires_internet
def test_source_not_removed_for_exit_plugins():
    this_file = inspect.getfile(PreRaises)
    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)
    watch_exit = Watcher()
    workflow = DockerBuildWorkflow(SOURCE, 'test-image',
                                   exit_plugins=[{'name': 'uses_source',
                                                  'args': {
                                                      'watcher': watch_exit,
                                                  }}],
                                   plugin_files=[this_file])

    workflow.build_docker_image()

    # Make sure that the plugin was actually run
    assert watch_exit.was_called()


class ValueMixIn(object):

    def __init__(self, tasker, workflow, *args, **kwargs):
        super(ValueMixIn, self).__init__(tasker, workflow, *args, **kwargs)

    def run(self):
        return '%s_result' % self.key


class PreBuildResult(ValueMixIn, PreBuildPlugin):
    """
    Pre build plugin that returns a result when run.
    """

    key = 'pre_build_value'


class PostBuildResult(ValueMixIn, PostBuildPlugin):
    """
    Post build plugin that returns a result when run.
    """

    key = 'post_build_value'


class PrePublishResult(ValueMixIn, PrePublishPlugin):
    """
    Pre publish plugin that returns a result when run.
    """

    key = 'pre_publish_value'


class ExitResult(ValueMixIn, ExitPlugin):
    """
    Exit plugin that returns a result when run.
    """

    key = 'exit_value'


def test_workflow_plugin_results():
    """
    Verifies the results of plugins in different phases
    are stored properly.
    """

    this_file = inspect.getfile(PreRaises)
    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)

    prebuild_plugins = [{'name': 'pre_build_value'}]
    postbuild_plugins = [{'name': 'post_build_value'}]
    prepublish_plugins = [{'name': 'pre_publish_value'}]
    exit_plugins = [{'name': 'exit_value'}]

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image',
                                   prebuild_plugins=prebuild_plugins,
                                   prepublish_plugins=prepublish_plugins,
                                   postbuild_plugins=postbuild_plugins,
                                   exit_plugins=exit_plugins,
                                   plugin_files=[this_file])

    workflow.build_docker_image()
    assert workflow.prebuild_results == {'pre_build_value': 'pre_build_value_result'}
    assert workflow.postbuild_results == {'post_build_value': 'post_build_value_result'}
    assert workflow.prepub_results == {'pre_publish_value': 'pre_publish_value_result'}
    assert workflow.exit_results == {'exit_value': 'exit_value_result'}

