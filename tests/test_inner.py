"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from collections import defaultdict
import json
import os

from atomic_reactor.build import InsideBuilder
from atomic_reactor.util import ImageName
from atomic_reactor.plugin import (PreBuildPlugin, PrePublishPlugin, PostBuildPlugin, ExitPlugin,
                                   AutoRebuildCanceledException, PluginFailedException,
                                   BuildCanceledException)
import atomic_reactor.plugin
import atomic_reactor.inner
import logging
from flexmock import flexmock
import pytest
from tests.constants import MOCK_SOURCE, SOURCE
from tests.docker_mock import mock_docker
from tests.util import requires_internet, is_string_type
import inspect
import signal
import threading

from time import sleep

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
    def __init__(self, failed=False, timeout=0):
        self.tasker = MockDockerTasker()
        self.base_image = ImageName(repo='Fedora', tag='22')
        self.image_id = 'asd'
        self.failed = failed
        self.timeout = timeout

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
        if self.timeout:
            sleep(self.timeout)

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


class WatcherWithSignal(Watcher):

    def __init__(self, signal=None):
        super(WatcherWithSignal, self).__init__()
        self.signal = signal

    def call(self):
        super(WatcherWithSignal, self).call()
        if self.signal:
            os.kill(os.getpid(), self.signal)


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


def test_workflow_compat(request):
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
    existing_logger = atomic_reactor.plugin.logger

    def restore_logger():
        atomic_reactor.plugin.logger = existing_logger

    request.addfinalizer(restore_logger)
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


class PrePub(PrePublishPlugin):
    """
    This plugin does nothing. It's only used for configuration testing.
    """

    key = 'prepub'


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

    # No 'name' key, prepub
    ({
        'prepublish_plugins': [{'args': {}},
                               {'name': 'prepub_watched',
                                'args': {
                                    'watcher': Watcher(),
                                },
                               }]},
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
                         }]
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

    # No 'args' key, prepub
    ({'prepublish_plugins': [{'name': 'prepub'},
                             {'name': 'prepub_watched',
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

    # No such plugin, prepub
    ({'prepublish_plugins': [{'name': 'no plugin',
                              'args': {}},
                             {'name': 'prepub_watched',
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

    # No such plugin, prebuild, not required
    ({'prebuild_plugins': [{'name': 'no plugin',
                            'args': {},
                            'required': False},
                           {'name': 'pre_watched',
                            'args': {
                                'watcher': Watcher(),
                            }
                           }]},
     False,  # not fatal
     False,  # does not log error
    ),

    # No such plugin, postbuild, not required
    ({'postbuild_plugins': [{'name': 'no plugin',
                             'args': {},
                             'required': False},
                            {'name': 'post_watched',
                             'args': {
                                 'watcher': Watcher(),
                             }
                            }]},
     False,  # not fatal
     False,  # does not log error
    ),

    # No such plugin, prepub, not required
    ({'prepublish_plugins': [{'name': 'no plugin',
                              'args': {},
                              'required': False},
                             {'name': 'prepub_watched',
                              'args': {
                                  'watcher': Watcher(),
                              }
                             }]},
     False,  # not fatal
     False,  # does not log error
    ),

    # No such plugin, exit, not required
    ({'exit_plugins': [{'name': 'no plugin',
                        'args': {},
                        'required': False},
                       {'name': 'exit_watched',
                        'args': {
                            'watcher': Watcher(),
                        }
                       }]},
     False,  # not fatal
     False,  # does not log error
    ),
])
def test_plugin_errors(request, plugins, should_fail, should_log):
    """
    Try bad plugin configuration.
    """

    this_file = inspect.getfile(PreRaises)
    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)
    fake_logger = FakeLogger()

    existing_logger = atomic_reactor.plugin.logger

    def restore_logger():
        atomic_reactor.plugin.logger = existing_logger

    request.addfinalizer(restore_logger)
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
        assert workflow.plugins_errors
        assert all([is_string_type(plugin)
                    for plugin in workflow.plugins_errors])
        assert all([is_string_type(reason)
                    for reason in workflow.plugins_errors.values()])
    else:
        workflow.build_docker_image()
        assert watcher.was_called()
        assert not workflow.plugins_errors

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


@pytest.mark.parametrize('fail_at', ['pre_raises',
                                     'prepub_raises',
                                     'post_raises',
                                     'exit_raises',
                                     'exit_raises_allowed'])
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
    if fail_at == 'pre_raises':
        prebuild_plugins.insert(0, {'name': fail_at, 'args': {}})
    elif fail_at == 'prepub_raises':
        prepublish_plugins.insert(0, {'name': fail_at, 'args': {}})
    elif fail_at == 'post_raises':
        postbuild_plugins.insert(0, {'name': fail_at, 'args': {}})
    elif fail_at == 'exit_raises' or fail_at == 'exit_raises_allowed':
        exit_plugins.insert(0, {'name': fail_at, 'args': {}})
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
    if fail_at == 'exit_raises_allowed':
        build_result = workflow.build_docker_image()
        assert build_result and not build_result.is_failed()
        assert not workflow.plugins_errors
    else:
        with pytest.raises(PluginFailedException):
            workflow.build_docker_image()

        assert fail_at in workflow.plugins_errors

    # The pre-build phase should only complete if there were no
    # earlier plugin failures.
    assert watch_pre.was_called() == (fail_at != 'pre_raises')

    # The prepublish phase should only complete if there were no
    # earlier plugin failures.
    assert watch_prepub.was_called() == (fail_at not in ('pre_raises',
                                                         'prepub_raises'))

    # The post-build phase should only complete if there were no
    # earlier plugin failures.
    assert watch_post.was_called() == (fail_at not in ('pre_raises',
                                                       'prepub_raises',
                                                       'post_raises'))

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


@pytest.mark.parametrize('fail_at', ['pre', 'prepub', 'build', 'post', 'exit'])
def test_cancel_build(request, fail_at):
    """
    Verifies that exit plugins are executed when the build is canceled
    """

    # Make the phase we're testing send us SIGTERM
    phase_signal = defaultdict(lambda: None)
    phase_signal[fail_at] = signal.SIGTERM

    this_file = inspect.getfile(PreRaises)
    mock_docker()
    build_timeout = 10 if fail_at == 'build' else 0
    sigterm_timeout = 2
    fake_builder = MockInsideBuilder(timeout=build_timeout)
    flexmock(InsideBuilder).new_instances(fake_builder)
    watch_pre = WatcherWithSignal(phase_signal['pre'])
    watch_prepub = WatcherWithSignal(phase_signal['prepub'])
    watch_post = WatcherWithSignal(phase_signal['post'])
    watch_exit = WatcherWithSignal(phase_signal['exit'])

    fake_logger = FakeLogger()
    existing_logger = atomic_reactor.plugin.logger

    def restore_logger():
        atomic_reactor.plugin.logger = existing_logger

    request.addfinalizer(restore_logger)
    atomic_reactor.plugin.logger = fake_logger

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

    if fail_at == 'build':
        pid = os.getpid()
        thread = threading.Thread(
            target=lambda: (
                sleep(sigterm_timeout),
                os.kill(pid, signal.SIGTERM)))
        thread.start()

        with pytest.raises(BuildCanceledException):
            workflow.build_docker_image()
    else:
        workflow.build_docker_image()

    if fail_at not in ['exit', 'build']:
        assert ("plugin '%s_watched' raised an exception:" % fail_at +
                " BuildCanceledException('Build was canceled',)",) in fake_logger.warnings

    assert watch_exit.was_called()
    assert watch_pre.was_called()

    if fail_at not in ['pre', 'build']:
        assert watch_prepub.was_called()

    if fail_at not in ['pre', 'prepub', 'build']:
        assert watch_post.was_called()


@pytest.mark.parametrize('has_version', [True, False])
def test_show_version(request, has_version):
    """
    Test atomic-reactor print version of osbs-client used to build the build json
    if available
    """
    VERSION = "1.0"

    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)

    fake_logger = FakeLogger()
    existing_logger = atomic_reactor.inner.logger

    def restore_logger():
        atomic_reactor.inner.logger = existing_logger

    request.addfinalizer(restore_logger)
    atomic_reactor.inner.logger = fake_logger

    params = {
        'prebuild_plugins': [],
        'prepublish_plugins': [],
        'postbuild_plugins': [],
        'exit_plugins': []
    }
    if has_version:
        params['client_version'] = VERSION

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image', **params)
    workflow.build_docker_image()

    expected_log_message = ("build json was built by osbs-client %s", VERSION)
    assert (expected_log_message in fake_logger.debugs) == has_version
