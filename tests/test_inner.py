"""
Copyright (c) 2015-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import logging
import json
import os
from collections import defaultdict
from dataclasses import fields, Field
from pathlib import Path

import time
from dockerfile_parse import DockerfileParser
from textwrap import dedent

import osbs.exceptions
from atomic_reactor.dirs import ContextDir
from atomic_reactor.plugin import (PreBuildPlugin, PrePublishPlugin, PostBuildPlugin, ExitPlugin,
                                   PluginFailedException,
                                   BuildStepPlugin, InappropriateBuildStepError)
from flexmock import flexmock
import pytest
from tests.util import is_string_type
from tests.constants import DOCKERFILE_MULTISTAGE_CUSTOM_BAD_PATH
import inspect
import signal

from atomic_reactor.inner import (BuildResults, BuildResultsEncoder,
                                  BuildResultsJSONDecoder, DockerBuildWorkflow,
                                  FSWatcher, ImageBuildWorkflowData, BuildResult, TagConf)
from atomic_reactor.constants import PLUGIN_BUILD_ORCHESTRATE_KEY
from atomic_reactor.source import PathSource, DummySource
from atomic_reactor.util import (
    DockerfileImages, df_parser, validate_with_schema, graceful_chain_get
)
from atomic_reactor.tasks.plugin_based import PluginsDef
from osbs.utils import ImageName


BUILD_RESULTS_ATTRS = ['build_logs',
                       'built_img_inspect',
                       'built_img_info',
                       'base_img_info',
                       'base_plugins_output',
                       'built_img_plugins_output']
DUMMY_BUILD_RESULT = BuildResult(image_id="image_id")
DUMMY_FAILED_BUILD_RESULT = BuildResult(fail_reason='it happens')
DUMMY_REMOTE_BUILD_RESULT = BuildResult.make_remote_image_result()

pytestmark = pytest.mark.usefixtures('user_params')


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
    for attr in set(BUILD_RESULTS_ATTRS) - {'build_logs'}:
        assert getattr(results, attr) == getattr(expected_results, attr)


class RaisesMixIn(object):
    """
    Mix-in class for plugins that should raise exceptions.
    """

    is_allowed_to_fail = False

    def __init__(self, workflow, *args, **kwargs):
        super(RaisesMixIn, self).__init__(workflow, *args, **kwargs)

    def run(self):
        raise RuntimeError


class PreRaises(RaisesMixIn, PreBuildPlugin):
    """
    This plugin must run and cause the build to abort.
    """

    key = 'pre_raises'


class BuildStepRaises(RaisesMixIn, BuildStepPlugin):
    """
    This plugin must run and cause the build to abort.
    """

    key = 'buildstep_raises'


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

    def __init__(self, workflow, watcher, *args, **kwargs):
        super(WatchedMixIn, self).__init__(workflow, *args, **kwargs)
        self.watcher = watcher

    def run(self):
        self.watcher.call()


class WatchedBuildStep(object):
    """
    class for buildstep plugins we want to watch.
    """

    def __init__(self, workflow, watcher, *args, **kwargs):
        super(WatchedBuildStep, self).__init__(workflow, *args, **kwargs)
        self.watcher = watcher

    def run(self):
        self.watcher.call()
        return DUMMY_BUILD_RESULT


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


class BuildStepWatched(WatchedBuildStep, BuildStepPlugin):
    """
    A BuildStep plugin we can watch.
    """

    key = 'buildstep_watched'


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
    def __init__(self, raise_exc=None):
        self.called = False
        self.raise_exc = raise_exc

    def call(self):
        self.called = True
        if self.raise_exc is not None:
            raise self.raise_exc    # pylint: disable=raising-bad-type

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


def test_workflow_base_images(build_dir):
    """
    Test workflow for base images
    """

    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreWatched)
    watch_pre = Watcher()
    watch_prepub = Watcher()
    watch_buildstep = Watcher()
    watch_post = Watcher()
    watch_exit = Watcher()
    workflow = DockerBuildWorkflow(
        build_dir,
        source=None,
        plugins=PluginsDef(
            prebuild=[{'name': 'pre_watched', 'args': {'watcher': watch_pre}}],
            buildstep=[{'name': 'buildstep_watched', 'args': {'watcher': watch_buildstep}}],
            prepublish=[{'name': 'prepub_watched', 'args': {'watcher': watch_prepub}}],
            postbuild=[{'name': 'post_watched', 'args': {'watcher': watch_post}}],
            exit=[{'name': 'exit_watched', 'args': {'watcher': watch_exit}}],
        ),
        plugin_files=[this_file],
    )

    workflow.build_docker_image()

    assert watch_pre.was_called()
    assert watch_prepub.was_called()
    assert watch_buildstep.was_called()
    assert watch_post.was_called()
    assert watch_exit.was_called()


def test_workflow_compat(build_dir, caplog):
    """
    Some of our plugins have changed from being run post-build to
    being run at exit. Let's test what happens when we try running an
    exit plugin as a post-build plugin.
    """
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreWatched)
    watch_exit = Watcher()
    watch_buildstep = Watcher()

    caplog.clear()

    workflow = DockerBuildWorkflow(
        build_dir,
        source=None,
        plugins=PluginsDef(
            postbuild=[{'name': 'store_logs_to_file', 'args': {'watcher': watch_exit}}],
            buildstep=[{'name': 'buildstep_watched', 'args': {'watcher': watch_buildstep}}],
        ),
        plugin_files=[this_file],
    )

    workflow.build_docker_image()
    assert watch_exit.was_called()
    for record in caplog.records:
        assert record.levelno != logging.ERROR


class Pre(PreBuildPlugin):
    """
    This plugin does nothing. It's only used for configuration testing.
    """

    key = 'pre'


class BuildStep(BuildStepPlugin):
    """
    This plugin does nothing. It's only used for configuration testing.
    """

    key = 'buildstep'

    def run(self):
        raise InappropriateBuildStepError


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
    # No 'args' key, prebuild
    ({'prebuild': [{'name': 'pre'}, {'name': 'pre_watched', 'args': {'watcher': Watcher()}}],
      'buildstep': [{'name': 'buildstep_watched', 'args': {'watcher': Watcher()}}]},
     False,  # not fatal
     False,  # no error logged
     ),

    # No 'args' key, buildstep
    ({'buildstep': [
        {'name': 'buildstep'}, {'name': 'buildstep_watched', 'args': {'watcher': Watcher()}}
    ]},
     False,  # not fatal
     False,  # no error logged
     ),

    # No 'args' key, postbuild
    ({'postbuild': [{'name': 'post'}, {'name': 'post_watched', 'args': {'watcher': Watcher()}}],
      'buildstep': [{'name': 'buildstep_watched', 'args': {'watcher': Watcher()}}]},
     False,  # not fatal,
     False,  # no error logged
     ),

    # No 'args' key, prepub
    ({'prepublish': [
        {'name': 'prepub'}, {'name': 'prepub_watched', 'args': {'watcher': Watcher()}}],
      'buildstep': [{'name': 'buildstep_watched', 'args': {'watcher': Watcher()}}]},
     False,  # not fatal,
     False,  # no error logged
     ),

    # No 'args' key, exit
    ({'exit': [{'name': 'exit'}, {'name': 'exit_watched', 'args': {'watcher': Watcher()}}],
      'buildstep': [{'name': 'buildstep_watched', 'args': {'watcher': Watcher()}}]},
     False,  # not fatal
     False,  # no error logged
     ),

    # No such plugin, prebuild
    ({'prebuild': [
        {'name': 'no plugin', 'args': {}},
        {'name': 'pre_watched', 'args': {'watcher': Watcher()}}]},
     True,  # is fatal
     True,  # logs error
     ),

    # No such plugin, buildstep
    ({'buildstep': [
        {'name': 'no plugin', 'args': {}},
        {'name': 'buildstep_watched', 'args': {'watcher': Watcher()}}]},
     False,  # is fatal
     False,  # logs error
     ),

    # No such plugin, postbuild
    ({'postbuild': [
        {'name': 'no plugin', 'args': {}},
        {'name': 'post_watched', 'args': {'watcher': Watcher()}}]},
     True,  # is fatal
     True,  # logs error
     ),

    # No such plugin, prepub
    ({'prepublish': [
        {'name': 'no plugin', 'args': {}},
        {'name': 'prepub_watched', 'args': {'watcher': Watcher()}}]},
     True,  # is fatal
     True,  # logs error
     ),

    # No such plugin, exit
    ({'exit': [
        {'name': 'no plugin', 'args': {}},
        {'name': 'exit_watched', 'args': {'watcher': Watcher()}}]},
     True,  # is fatal
     True,   # logs error
     ),

    # No such plugin, prebuild, not required
    ({'prebuild': [
        {'name': 'no plugin', 'args': {}, 'required': False},
        {'name': 'pre_watched', 'args': {'watcher': Watcher()}}],
      'buildstep': [{'name': 'buildstep_watched', 'args': {'watcher': Watcher()}}]},
     False,  # not fatal
     False,  # does not log error
     ),

    # No such plugin, buildstep, not required
    ({'buildstep': [
        {'name': 'no plugin', 'args': {}, 'required': False},
        {'name': 'buildstep_watched', 'args': {'watcher': Watcher()}}]},
     False,  # not fatal
     False,  # does not log error
     ),

    # No such plugin, postbuild, not required
    ({'postbuild': [
        {'name': 'no plugin', 'args': {}, 'required': False},
        {'name': 'post_watched', 'args': {'watcher': Watcher()}}],
      'buildstep': [{'name': 'buildstep_watched', 'args': {'watcher': Watcher()}}]},
     False,  # not fatal
     False,  # does not log error
     ),

    # No such plugin, prepub, not required
    ({'prepublish': [
        {'name': 'no plugin', 'args': {}, 'required': False},
        {'name': 'prepub_watched', 'args': {'watcher': Watcher()}}],
      'buildstep': [{'name': 'buildstep_watched', 'args': {'watcher': Watcher()}}]},
     False,  # not fatal
     False,  # does not log error
     ),

    # No such plugin, exit, not required
    ({'exit': [
        {'name': 'no plugin', 'args': {}, 'required': False},
        {'name': 'exit_watched', 'args': {'watcher': Watcher()}}],
      'buildstep': [{'name': 'buildstep_watched', 'args': {'watcher': Watcher()}}]},
     False,  # not fatal
     False,  # does not log error
     ),
])
def test_plugin_errors(plugins, should_fail, should_log, build_dir, caplog):
    """
    Try bad plugin configuration.
    """
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreRaises)

    caplog.clear()
    workflow = DockerBuildWorkflow(build_dir,
                                   source=None,
                                   plugin_files=[this_file],
                                   plugins=PluginsDef(**plugins))

    # Find the 'watcher' parameter
    watchers = [conf.get('args', {}).get('watcher')
                for plugin in plugins.values()
                for conf in plugin]
    watcher = [x for x in watchers if x][0]

    if should_fail:
        with pytest.raises(PluginFailedException):
            workflow.build_docker_image()

        assert not watcher.was_called()
        assert workflow.data.plugins_errors
        assert all([is_string_type(plugin)
                    for plugin in workflow.data.plugins_errors])
        assert all([is_string_type(reason)
                    for reason in workflow.data.plugins_errors.values()])
    else:
        workflow.build_docker_image()
        assert watcher.was_called()
        assert not workflow.data.plugins_errors

    if should_log:
        assert any(record.levelno == logging.ERROR for record in caplog.records)
    else:
        assert all(record.levelno != logging.ERROR for record in caplog.records)


@pytest.mark.parametrize('fail_at', ['pre_raises',
                                     'buildstep_raises',
                                     'prepub_raises',
                                     'post_raises',
                                     'exit_raises',
                                     'exit_raises_allowed'])
def test_workflow_plugin_error(fail_at, build_dir):
    """
    This is a test for what happens when plugins fail.

    When a prebuild or postbuild plugin fails, and doesn't have
    is_allowed_to_fail=True set, the whole build should fail.
    However, all the exit plugins should run.
    """
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreRaises)
    watch_pre = Watcher()
    watch_prepub = Watcher()
    watch_buildstep = Watcher()
    watch_post = Watcher()
    watch_exit = Watcher()
    plugins = PluginsDef(
        prebuild=[{'name': 'pre_watched', 'args': {'watcher': watch_pre}}],
        buildstep=[{'name': 'buildstep_watched', 'args': {'watcher': watch_buildstep}}],
        prepublish=[{'name': 'prepub_watched', 'args': {'watcher': watch_prepub}}],
        postbuild=[{'name': 'post_watched', 'args': {'watcher': watch_post}}],
        exit=[{'name': 'exit_watched', 'args': {'watcher': watch_exit}}],
    )

    # Insert a failing plugin into one of the build phases
    if fail_at == 'pre_raises':
        plugins.prepublish.insert(0, {'name': fail_at, 'args': {}})
    elif fail_at == 'buildstep_raises':
        plugins.buildstep.insert(0, {'name': fail_at, 'args': {}})
    elif fail_at == 'prepub_raises':
        plugins.prepublish.insert(0, {'name': fail_at, 'args': {}})
    elif fail_at == 'post_raises':
        plugins.postbuild.insert(0, {'name': fail_at, 'args': {}})
    elif fail_at == 'exit_raises' or fail_at == 'exit_raises_allowed':
        plugins.exit.insert(0, {'name': fail_at, 'args': {}})
    else:
        # Typo in the parameter list?
        assert False

    workflow = DockerBuildWorkflow(
        build_dir, source=None, plugins=plugins, plugin_files=[this_file]
    )

    # Most failures cause the build process to abort. Unless, it's
    # an exit plugin that's explicitly allowed to fail.
    if fail_at == 'exit_raises_allowed':
        workflow.build_docker_image()
        assert not workflow.data.plugins_errors
    else:
        with pytest.raises(PluginFailedException):
            workflow.build_docker_image()

        assert fail_at in workflow.data.plugins_errors

    # The pre-build phase should only complete if there were no
    # earlier plugin failures.
    assert watch_pre.was_called() == (fail_at != 'pre_raises')

    # The buildstep phase should only complete if there were no
    # earlier plugin failures.
    assert watch_buildstep.was_called() == (fail_at not in ('pre_raises',
                                                            'buildstep_raises'))

    # The prepublish phase should only complete if there were no
    # earlier plugin failures.
    assert watch_prepub.was_called() == (fail_at not in ('pre_raises',
                                                         'prepub_raises',
                                                         'buildstep_raises'))

    # The post-build phase should only complete if there were no
    # earlier plugin failures.
    assert watch_post.was_called() == (fail_at not in ('pre_raises',
                                                       'prepub_raises',
                                                       'buildstep_raises',
                                                       'post_raises'))

    # But all exit plugins should run, even if one of them also raises
    # an exception.
    assert watch_exit.was_called()


def test_workflow_docker_build_error(build_dir):
    """
    This is a test for what happens when the docker build fails.
    """
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreRaises)
    watch_pre = Watcher()
    watch_buildstep = Watcher(raise_exc=Exception())
    watch_prepub = Watcher()
    watch_post = Watcher()
    watch_exit = Watcher()

    workflow = DockerBuildWorkflow(
        build_dir,
        source=None,
        plugins=PluginsDef(
            prebuild=[{'name': 'pre_watched', 'args': {'watcher': watch_pre}}],
            buildstep=[{'name': 'buildstep_watched', 'args': {'watcher': watch_buildstep}}],
            prepublish=[{'name': 'prepub_watched', 'args': {'watcher': watch_prepub}}],
            postbuild=[{'name': 'post_watched', 'args': {'watcher': watch_post}}],
            exit=[{'name': 'exit_watched', 'args': {'watcher': watch_exit}}],
        ),
        plugin_files=[this_file],
    )

    with pytest.raises(Exception):
        workflow.build_docker_image()
    # No subsequent build phases should have run except 'exit'
    assert watch_pre.was_called()
    assert watch_buildstep.was_called()
    assert not watch_prepub.was_called()
    assert not watch_post.was_called()
    assert watch_exit.was_called()


@pytest.mark.parametrize('steps_to_fail,step_reported', (
    # single failures
    ({'pre'}, 'pre'),
    ({'buildstep'}, 'buildstep'),
    ({'prepub'}, 'prepub'),
    ({'post'}, 'post'),
    ({'exit'}, 'exit'),
    # non-exit + exit failure
    ({'pre', 'exit'}, 'pre'),
    ({'buildstep', 'exit'}, 'buildstep'),
    ({'prepub', 'exit'}, 'prepub'),
    ({'post', 'exit'}, 'post'),
    # 2 non-exit failures
    ({'pre', 'buildstep'}, 'pre'),
    ({'pre', 'prepub'}, 'pre'),
    ({'pre', 'post'}, 'pre'),
    ({'buildstep', 'prepub'}, 'buildstep'),
    ({'buildstep', 'post'}, 'buildstep'),
    ({'prepub', 'post'}, 'prepub'),
))
def test_workflow_docker_build_error_reports(steps_to_fail, step_reported, build_dir):
    """
    Test if first error is reported properly. (i.e. exit plugins are not
    hiding the original root cause)
    """
    def exc_string(step):
        return 'test_workflow_docker_build_error_reports.{}'.format(step)

    def construct_watcher(step):
        watcher = Watcher(raise_exc=Exception(exc_string(step)) if step in steps_to_fail else None)
        return watcher

    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreRaises)
    watch_pre = construct_watcher('pre')
    watch_buildstep = construct_watcher('buildstep')
    watch_prepub = construct_watcher('prepub')
    watch_post = construct_watcher('post')
    watch_exit = construct_watcher('exit')

    workflow = DockerBuildWorkflow(
        build_dir,
        source=None,
        plugins=PluginsDef(
            prebuild=[{'name': 'pre_watched',
                       'is_allowed_to_fail': False,
                       'args': {'watcher': watch_pre}}],
            buildstep=[{'name': 'buildstep_watched',
                        'is_allowed_to_fail': False,
                        'args': {'watcher': watch_buildstep}}],
            prepublish=[{'name': 'prepub_watched',
                         'is_allowed_to_fail': False,
                         'args': {'watcher': watch_prepub}}],
            postbuild=[{'name': 'post_watched',
                        'is_allowed_to_fail': False,
                        'args': {'watcher': watch_post}}],
            exit=[{'name': 'exit_watched',
                   'is_allowed_to_fail': False,
                   'args': {'watcher': watch_exit}}],
        ),
        plugin_files=[this_file],
    )

    with pytest.raises(Exception) as exc:
        workflow.build_docker_image()
    assert exc_string(step_reported) in str(exc.value)


class ExitUsesSource(ExitWatched):
    key = 'uses_source'

    def run(self):
        assert os.path.exists(self.workflow.source.get_build_file_path()[0])
        WatchedMixIn.run(self)


def test_source_not_removed_for_exit_plugins(build_dir):
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreRaises)
    watch_exit = Watcher()
    watch_buildstep = Watcher()
    workflow = DockerBuildWorkflow(
        build_dir,
        source=None,
        plugins=PluginsDef(
            exit=[{'name': 'uses_source', 'args': {'watcher': watch_exit}}],
            buildstep=[{'name': 'buildstep_watched', 'args': {'watcher': watch_buildstep}}],
        ),
        plugin_files=[this_file],
    )

    workflow.build_docker_image()

    # Make sure that the plugin was actually run
    assert watch_exit.was_called()


class ValueMixIn(object):

    def __init__(self, workflow, *args, **kwargs):
        super(ValueMixIn, self).__init__(workflow, *args, **kwargs)

    def run(self):
        return '%s_result' % self.key


class ValueBuildStep(object):

    def __init__(self, workflow, *args, **kwargs):
        super(ValueBuildStep, self).__init__(workflow, *args, **kwargs)

    def run(self):
        return DUMMY_BUILD_RESULT


class ValueFailedBuildStep(object):

    def run(self):
        return DUMMY_FAILED_BUILD_RESULT


class ValueRemoteBuildStep(object):

    def run(self):
        return DUMMY_REMOTE_BUILD_RESULT


class PreBuildResult(ValueMixIn, PreBuildPlugin):
    """
    Pre build plugin that returns a result when run.
    """

    key = 'pre_build_value'


class BuildStepResult(ValueBuildStep, BuildStepPlugin):
    """
    Build step plugin that returns a result when run.
    """

    key = 'buildstep_value'


class New_BuildStepResult(ValueBuildStep, BuildStepPlugin):
    """
    New Build step plugin that returns a result when run.
    """

    key = 'imagebuilder'


class Old_BuildStepResult(ValueBuildStep, BuildStepPlugin):
    """
    Old Build step plugin that returns a result when run.
    """

    key = 'docker_api'


class BuildStepFailedResult(ValueFailedBuildStep, BuildStepPlugin):
    """
    Build step plugin that returns a failed result when run.
    """

    key = 'buildstep_failed_value'


class BuildStepRemoteResult(ValueRemoteBuildStep, BuildStepPlugin):
    """
    Build step plugin that returns a failed result when run.
    """

    key = 'buildstep_remote_value'


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


@pytest.mark.parametrize(['buildstep_plugin', 'buildstep_raises'], [
    ['buildstep_value', False],
    ['buildstep_remote_value', False],
    ['buildstep_failed_value', True],
])
def test_workflow_plugin_results(buildstep_plugin, buildstep_raises, build_dir):
    """
    Verifies the results of plugins in different phases
    are stored properly.
    It also verifies failed and remote BuildResult is handled properly.
    """

    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreRaises)

    plugins = PluginsDef(
        prebuild=[{'name': 'pre_build_value'}],
        buildstep=[{'name': buildstep_plugin}],
        postbuild=[{'name': 'post_build_value'}],
        prepublish=[{'name': 'pre_publish_value'}],
        exit=[{'name': 'exit_value'}],
    )

    workflow = DockerBuildWorkflow(
        build_dir, source=None, plugins=plugins, plugin_files=[this_file]
    )

    if buildstep_raises:
        with pytest.raises(PluginFailedException):
            workflow.build_docker_image()
    else:
        workflow.build_docker_image()

    assert workflow.data.prebuild_results == {'pre_build_value': 'pre_build_value_result'}
    assert isinstance(workflow.data.buildstep_result[buildstep_plugin], BuildResult)

    if buildstep_raises:
        assert workflow.data.postbuild_results == {}
        assert workflow.data.prepub_results == {}
    else:
        assert workflow.data.postbuild_results == {'post_build_value': 'post_build_value_result'}
        assert workflow.data.prepub_results == {'pre_publish_value': 'pre_publish_value_result'}

    assert workflow.data.exit_results == {'exit_value': 'exit_value_result'}


def test_parse_dockerfile_again_after_data_is_loaded(build_dir, tmpdir):
    context_dir = ContextDir(Path(tmpdir.join("context_dir")))
    wf_data = ImageBuildWorkflowData.load_from_dir(context_dir)
    # Note that argument source is None, that causes a DummySource is created
    # and "FROM scratch" is included in the Dockerfile.
    workflow = DockerBuildWorkflow(build_dir, wf_data)
    assert ["scratch"] == workflow.data.dockerfile_images.original_parents

    # Now, save the workflow data and load it again
    wf_data.save(context_dir)

    another_source = DummySource("git", "https://git.host/")
    dfp = df_parser(another_source.source_path)
    dfp.content = 'FROM fedora:35\nCMD ["bash", "--version"]'

    wf_data = ImageBuildWorkflowData.load_from_dir(context_dir)
    flexmock(DockerBuildWorkflow).should_receive("_parse_dockerfile_images").never()
    flexmock(wf_data.dockerfile_images).should_receive("set_source_registry").never()
    workflow = DockerBuildWorkflow(build_dir, wf_data, source=another_source)
    assert ["scratch"] == workflow.data.dockerfile_images.original_parents, \
        "The dockerfile_images should not be changed."


@pytest.mark.parametrize('fail_at', ['pre', 'prepub', 'buildstep', 'post', 'exit'])
def test_cancel_build(fail_at, build_dir, caplog):
    """
    Verifies that exit plugins are executed when the build is canceled
    """
    # Make the phase we're testing send us SIGTERM
    phase_signal = defaultdict(lambda: None)
    phase_signal[fail_at] = signal.SIGTERM
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreRaises)
    watch_pre = WatcherWithSignal(phase_signal['pre'])
    watch_prepub = WatcherWithSignal(phase_signal['prepub'])
    watch_buildstep = WatcherWithSignal(phase_signal['buildstep'])
    watch_post = WatcherWithSignal(phase_signal['post'])
    watch_exit = WatcherWithSignal(phase_signal['exit'])

    caplog.clear()

    workflow = DockerBuildWorkflow(
        build_dir,
        source=None,
        plugins=PluginsDef(
            prebuild=[{'name': 'pre_watched', 'args': {'watcher': watch_pre}}],
            prepublish=[{'name': 'prepub_watched', 'args': {'watcher': watch_prepub}}],
            buildstep=[{'name': 'buildstep_watched', 'args': {'watcher': watch_buildstep}}],
            postbuild=[{'name': 'post_watched', 'args': {'watcher': watch_post}}],
            exit=[{'name': 'exit_watched', 'args': {'watcher': watch_exit}}],
        ),
        plugin_files=[this_file],
    )
    # BaseException repr does not include trailing comma in Python >= 3.7
    # we look for a partial match in log strings for Python < 3.7 compatibility
    expected_entry = (
        "plugin '{}_watched' raised an exception: BuildCanceledException: Build was canceled"
    )
    if fail_at == 'buildstep':
        with pytest.raises(PluginFailedException):
            workflow.build_docker_image()
        assert workflow.data.build_canceled
        assert any(
            expected_entry.format(fail_at) in record.message
            for record in caplog.records
            if record.levelno == logging.ERROR
        )
    else:
        workflow.build_docker_image()

        if fail_at != 'exit':
            assert workflow.data.build_canceled
            assert any(
                expected_entry.format(fail_at) in record.message
                for record in caplog.records
                if record.levelno == logging.WARNING
            )
        else:
            assert not workflow.data.build_canceled

    assert watch_exit.was_called()
    assert watch_pre.was_called()

    if fail_at not in ['pre', 'buildstep']:
        assert watch_prepub.was_called()

    if fail_at not in ['pre', 'prepub', 'buildstep']:
        assert watch_post.was_called()


@pytest.mark.parametrize('has_version', [True, False])
def test_show_version(has_version, build_dir, caplog):
    """
    Test atomic-reactor print version of osbs-client used to build the build json
    if available
    """
    VERSION = "1.0"
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreRaises)
    watch_buildstep = Watcher()

    caplog.clear()

    plugins = PluginsDef(
        buildstep=[{'name': 'buildstep_watched', 'args': {'watcher': watch_buildstep}}],
    )
    params = {
        'plugins': plugins,
        'plugin_files': [this_file],
    }
    if has_version:
        params['client_version'] = VERSION

    workflow = DockerBuildWorkflow(build_dir, source=None, **params)
    workflow.build_docker_image()
    expected_log_message = "build json was built by osbs-client {}".format(VERSION)
    assert any(
        expected_log_message in record.message
        for record in caplog.records
        if record.levelno == logging.DEBUG
    ) == has_version


@pytest.mark.parametrize('buildstep_plugins, is_orchestrator', [
    ([], False),
    ([{'name': 'some_name'}], False),
    ([{'name': PLUGIN_BUILD_ORCHESTRATE_KEY}], True),

    ([{'name': 'some_other_name'},
      {'name': PLUGIN_BUILD_ORCHESTRATE_KEY}], True)
])
def test_workflow_is_orchestrator_build(buildstep_plugins, is_orchestrator, build_dir):
    workflow = DockerBuildWorkflow(build_dir,
                                   source=None,
                                   plugins=PluginsDef(buildstep=buildstep_plugins))
    assert workflow.is_orchestrator_build() == is_orchestrator


def test_parent_images_to_str(caplog, build_dir):
    workflow = DockerBuildWorkflow(build_dir, source=None)
    workflow.data.dockerfile_images = DockerfileImages(['fedora:latest', 'bacon'])
    workflow.data.dockerfile_images['fedora:latest'] = "spam"
    expected_results = {
        "fedora:latest": "spam:latest"
    }
    assert workflow.parent_images_to_str() == expected_results
    assert "None in: base bacon:latest has parent None" in caplog.text


def test_no_base_image(build_dir):
    source = DummySource("git", "https://git.host/")
    dfp = df_parser(source.source_path)
    dfp.content = "# no FROM\nADD spam /eggs"
    with pytest.raises(RuntimeError, match="no base image specified"):
        DockerBuildWorkflow(build_dir, source=source)


def test_different_custom_base_images(build_dir, source_dir):
    source = PathSource(
        "path", f"file://{DOCKERFILE_MULTISTAGE_CUSTOM_BAD_PATH}", workdir=str(source_dir)
    )
    with pytest.raises(NotImplementedError) as exc:
        DockerBuildWorkflow(build_dir, source=source)
    message = "multiple different custom base images aren't allowed in Dockerfile"
    assert message in str(exc.value)


def test_copy_from_unkown_stage(build_dir, source_dir):
    """test when user has specified COPY --from=image (instead of builder)"""
    source = PathSource("path", f"file://{source_dir}", workdir=str(source_dir))

    dfp = df_parser(str(source_dir))
    dfp.content = dedent("""\
        FROM monty as vikings
        FROM python
        # using a stage name we haven't seen should break:
        COPY --from=notvikings /spam/eggs /bin/eggs
    """)
    with pytest.raises(RuntimeError) as exc_info:
        DockerBuildWorkflow(build_dir, source=source)
    assert "FROM notvikings AS source" in str(exc_info.value)


def test_copy_from_invalid_index(build_dir, source_dir):
    source = PathSource("path", f"file://{source_dir}", workdir=str(source_dir))

    dfp = df_parser(str(source_dir))
    dfp.content = dedent("""\
        FROM monty as vikings
        # using an index we haven't seen should break:
        COPY --from=5 /spam/eggs /bin/eggs
    """)
    with pytest.raises(RuntimeError) as exc_info:
        DockerBuildWorkflow(build_dir, source=source)
    assert "COPY --from=5" in str(exc_info.value)


def test_fs_watcher_update(monkeypatch):

    # check that using the actual os call does not choke
    assert type(FSWatcher._update({})) is dict

    # check that the data actually gets updated
    stats = flexmock(
        f_frsize=1000,  # pretend blocks are 1000 bytes to make mb come out right
        f_blocks=101 * 1000,
        f_bfree=99 * 1000,
        f_files=1, f_ffree=1,
    )
    data = dict(mb_total=101, mb_free=100)
    monkeypatch.setattr(os, "statvfs", stats)
    assert type(FSWatcher._update(data)) is dict
    assert data["mb_used"] == 2
    assert data["mb_free"] == 99


def test_fs_watcher(monkeypatch):
    w = FSWatcher()
    monkeypatch.setattr(time, "sleep", lambda x: x)  # don't waste a second of test time
    w.start()
    w.finish()
    w.join(0.1)  # timeout if thread still running
    assert not w.is_alive()
    assert "mb_used" in w.get_usage_data()


def test_build_result():
    with pytest.raises(AssertionError):
        BuildResult(fail_reason='it happens', image_id='spam')


class TestBuildResult:
    """Test class BuildResult"""

    def test_build_result(self):
        with pytest.raises(AssertionError):
            BuildResult(fail_reason='it happens', image_id='spam')

        with pytest.raises(AssertionError):
            BuildResult(fail_reason='', image_id='spam')

        with pytest.raises(AssertionError):
            BuildResult(fail_reason='it happens', source_docker_archive='/somewhere')

        with pytest.raises(AssertionError):
            BuildResult(image_id='spam', source_docker_archive='/somewhere')

        with pytest.raises(AssertionError):
            BuildResult(image_id='spam',
                        fail_reason='it happens',
                        source_docker_archive='/somewhere')

        assert BuildResult(fail_reason='it happens').is_failed()
        assert not BuildResult(image_id='spam').is_failed()

        assert BuildResult(image_id='spam', logs=list('logs')).logs == list('logs')

        assert BuildResult(fail_reason='it happens').fail_reason == 'it happens'
        assert BuildResult(image_id='spam').image_id == 'spam'

        annotations = {'ham': 'mah'}
        assert BuildResult(image_id='spam', annotations=annotations).annotations == annotations

        assert BuildResult(image_id='spam', labels={'ham': 'mah'}).labels == {'ham': 'mah'}

        assert BuildResult(source_docker_archive='/somewhere').source_docker_archive == '/somewhere'

        assert BuildResult(image_id='spam').is_image_available()
        assert not BuildResult(fail_reason='it happens').is_image_available()
        assert not BuildResult.make_remote_image_result().is_image_available()

        assert not BuildResult.make_remote_image_result().is_failed()

    def test_as_dict(self):
        build_result = BuildResult(
            logs=["start build", "fetch sources"],
            image_id="registry/image:1.2",
            annotations={"name": "app"},
        )
        expected = {
            "annotations": {"name": "app"},
            "fail_reason": None,
            "image_id": "registry/image:1.2",
            "labels": None,
            "logs": ["start build", "fetch sources"],
            "skip_layer_squash": False,
            "source_docker_archive": None,
        }
        assert expected == build_result.as_dict()

    def test_parse(self):
        input_data = {
            "image_id": "image_id",
            "labels": {"label1": "value1", "label2": "value2"},
        }
        br = BuildResult.load(input_data)
        assert input_data["image_id"] == br.image_id
        assert input_data["labels"] == br.labels
        assert br.fail_reason is None
        assert br.logs == []
        assert br.annotations is None
        assert not br.skip_layer_squash
        assert br.source_docker_archive is None


class TestTagConf:
    """Test class TagConf"""

    def test_dump_empty_object(self):
        expected = {
            'primary_images': [],
            'unique_images': [],
            'floating_images': [],
        }
        assert expected == TagConf().as_dict()

    def test_as_dict(self):
        tag_conf = TagConf()
        tag_conf.add_primary_image('r.fp.o/f:35')
        tag_conf.add_floating_image('ns/img:latest')
        tag_conf.add_floating_image('ns1/img2:devel')
        expected = {
            'primary_images': [ImageName.parse('r.fp.o/f:35')],
            'unique_images': [],
            'floating_images': [
                ImageName.parse('ns/img:latest'),
                ImageName.parse('ns1/img2:devel'),
            ],
        }
        assert expected == tag_conf.as_dict()

    @pytest.mark.parametrize(
        'input_data,expected_primary_images,expected_unique_images,expected_floating_images',
        [
            [
                {
                    'primary_images': ['registry/image:2.4'],
                    'unique_images': ['registry/image:2.4'],
                    'floating_images': ['registry/image:latest'],
                },
                [ImageName.parse('registry/image:2.4')],
                [ImageName.parse('registry/image:2.4')],
                [ImageName.parse('registry/image:latest')],
            ],
            [
                {
                    'primary_images': [],
                    'unique_images': [],
                    'floating_images': ['registry/image:latest', 'registry/image:devel'],
                },
                [],
                [],
                [
                    ImageName.parse('registry/image:latest'),
                    ImageName.parse('registry/image:devel'),
                ],
            ],
            [
                {'floating_images': ['registry/image:latest']},
                [],
                [],
                [ImageName.parse('registry/image:latest')],
            ],
        ],
    )
    def test_parse_images(
        self, input_data, expected_primary_images, expected_unique_images, expected_floating_images
    ):
        tag_conf = TagConf.load(input_data)
        assert expected_primary_images == tag_conf.primary_images
        assert expected_unique_images == tag_conf.unique_images
        assert expected_floating_images == tag_conf.floating_images

    def test_get_unique_images_with_platform(self):
        image = 'registry.com/org/hello:world-16111-20220103213046'
        platform = 'x86_64'

        tag_conf = TagConf()
        tag_conf.add_unique_image(image)

        expected = [ImageName.parse(f'{image}-{platform}')]
        actual = tag_conf.get_unique_images_with_platform(platform)

        assert actual == expected


class TestWorkflowData:
    """Test class ImageBuildWorkflowData."""

    def test_creation(self):
        data = ImageBuildWorkflowData()
        assert data.dockerfile_images.is_empty
        assert data.tag_conf.is_empty
        assert data.build_result is None
        assert {} == data.prebuild_results

    def test_load_from_empty_dump(self):
        wf_data = ImageBuildWorkflowData.load({})
        empty_data = ImageBuildWorkflowData()
        field: Field
        for field in fields(ImageBuildWorkflowData):
            name = field.name
            assert getattr(empty_data, name) == getattr(wf_data, name)

    def test_load_from_dump(self):
        input_data = {
            "dockerfile_images": {
                "original_parents": ["scratch"],
                "local_parents": [],
                "source_registry": None,
                "organization": None,
            },
            "prebuild_results": {"plugin_1": "result"},
            "tag_conf": {
                "floating_images": [
                    ImageName.parse("registry/httpd:2.4").to_str(),
                ],
            },
        }
        wf_data = ImageBuildWorkflowData.load(input_data)

        expected_df_images = DockerfileImages.load(input_data["dockerfile_images"])
        assert expected_df_images == wf_data.dockerfile_images
        assert input_data["prebuild_results"] == wf_data.prebuild_results
        assert TagConf.load(input_data["tag_conf"]) == wf_data.tag_conf

    def test_load_from_empty_directory(self, tmpdir):
        context_dir = tmpdir.join("context_dir").mkdir()
        # Note: no data file is created here, e.g. workflow.json.
        wf_data = ImageBuildWorkflowData.load_from_dir(ContextDir(context_dir))
        assert wf_data.dockerfile_images.is_empty
        assert wf_data.tag_conf.is_empty
        assert {} == wf_data.prebuild_results

    @pytest.mark.parametrize("data_path,prop_name,wrong_value", [
        # digests should map to an object rather than a string
        [["tag_conf"], "original_parents", "wrong value"],
        # tag name should map to an object rather than a string
        [["tag_conf"], "floating_images", "wrong value"],
    ])
    def test_load_invalid_data_from_directory(self, data_path, prop_name, wrong_value, tmpdir):
        """Test the workflow data is validated by JSON schema when reading from context_dir."""
        context_dir = ContextDir(Path(tmpdir.join("context_dir").mkdir()))

        data = ImageBuildWorkflowData(dockerfile_images=DockerfileImages(["scratch"]))
        data.tag_conf.add_floating_image("registry/httpd:2.4")
        data.prebuild_results["plugin_1"] = "result"
        data.save(context_dir)

        saved_data = json.loads(context_dir.workflow_json.read_bytes())
        # Make data invalid
        graceful_chain_get(saved_data, *data_path, make_copy=False)[prop_name] = wrong_value
        context_dir.workflow_json.write_text(json.dumps(saved_data), encoding="utf-8")

        with pytest.raises(osbs.exceptions.OsbsValidationException):
            ImageBuildWorkflowData.load_from_dir(context_dir)

    def test_save_and_load(self, tmpdir):
        """Test save workflow data and then load them back properly."""
        tag_conf = TagConf()
        tag_conf.add_floating_image(ImageName.parse("registry/image:latest"))
        tag_conf.add_primary_image(ImageName.parse("registry/image:1.0"))

        wf_data = ImageBuildWorkflowData(
            dockerfile_images=DockerfileImages(["scratch", "registry/f:35"]),
            # Test object in dict values is serialized
            buildstep_result={"image_build": BuildResult(logs=["Build succeeds."])},
            postbuild_results={
                "tag_and_push": [
                    # Such object in a list should be handled properly.
                    ImageName(registry="localhost:5000", repo='image', tag='latest'),
                ]
            },
            tag_conf=tag_conf,
            prebuild_results={
                "plugin_a": {
                    'parent-images-koji-builds': {
                        ImageName(repo='base', tag='latest').to_str(): {
                            'id': 123456789,
                            'nvr': 'base-image-1.0-99',
                            'state': 1,
                        },
                    },
                },
            },
        )

        context_dir = ContextDir(Path(tmpdir.join("context_dir").mkdir()))
        wf_data.save(context_dir)

        assert context_dir.workflow_json.exists()

        # Verify the saved data matches the schema
        saved_data = json.loads(context_dir.workflow_json.read_bytes())
        try:
            validate_with_schema(saved_data, "schemas/workflow_data.json")
        except osbs.exceptions.OsbsValidationException as e:
            pytest.fail(f"The dumped workflow data does not match JSON schema: {e}")

        # Load and verify the loaded data
        loaded_wf_data = ImageBuildWorkflowData.load_from_dir(context_dir)

        assert wf_data.dockerfile_images == loaded_wf_data.dockerfile_images
        assert wf_data.tag_conf == loaded_wf_data.tag_conf
        assert wf_data.buildstep_result == loaded_wf_data.buildstep_result
        assert wf_data.postbuild_results == loaded_wf_data.postbuild_results
        assert wf_data.prebuild_results == loaded_wf_data.prebuild_results
