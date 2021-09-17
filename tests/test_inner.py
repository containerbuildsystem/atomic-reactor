"""
Copyright (c) 2015, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from collections import defaultdict
import logging
import json
import os
import time
import docker
from dockerfile_parse import DockerfileParser
from textwrap import dedent

from atomic_reactor.build import InsideBuilder, BuildResult
from atomic_reactor.plugin import (PreBuildPlugin, PrePublishPlugin, PostBuildPlugin, ExitPlugin,
                                   PluginFailedException,
                                   BuildStepPlugin, InappropriateBuildStepError)
from flexmock import flexmock
import pytest
from tests.docker_mock import mock_docker
from tests.util import requires_internet, is_string_type
from tests.constants import DOCKERFILE_MULTISTAGE_CUSTOM_BAD_PATH
import inspect
import signal

from atomic_reactor.inner import (BuildResults, BuildResultsEncoder,
                                  BuildResultsJSONDecoder, DockerBuildWorkflow,
                                  FSWatcher, PushConf, DockerRegistry)
from atomic_reactor.constants import (INSPECT_ROOTFS,
                                      INSPECT_ROOTFS_LAYERS,
                                      PLUGIN_BUILD_ORCHESTRATE_KEY)
from atomic_reactor.util import DockerfileImages, df_parser


BUILD_RESULTS_ATTRS = ['build_logs',
                       'built_img_inspect',
                       'built_img_info',
                       'base_img_info',
                       'base_plugins_output',
                       'built_img_plugins_output']
DUMMY_BUILD_RESULT = BuildResult(image_id="image_id")
DUMMY_FAILED_BUILD_RESULT = BuildResult(fail_reason='it happens')
DUMMY_REMOTE_BUILD_RESULT = BuildResult.make_remote_image_result()
DUMMY_ORIGINAL_DF = "FROM test_base_image"

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


class MockDockerTasker(object):
    def inspect_image(self, name):
        return {}

    def build_image_from_path(self):
        return True

    def get_image_history(self, name):
        return [{'Size': 1, 'Id': "sha256:layer1-newest"},
                {'Size': 2, 'Id': "sha256:layer2"},
                {'Size': 3, 'Id': "sha256:layer3"},
                {'Size': 4, 'Id': "sha256:layer4-oldest"}]


class MockDockerTaskerBaseImage(MockDockerTasker):
    def inspect_image(self, name):
        raise docker.errors.NotFound(message='foo', response='bar', explanation='baz')


class MockInsideBuilder(object):
    def __init__(self, failed=False, is_base_image=False):
        if is_base_image:
            self.tasker = MockDockerTaskerBaseImage()
        else:
            self.tasker = MockDockerTasker()
        self.dockerfile_images = None
        self.image_id = 'asd'
        self.image = 'image'
        self.failed = failed
        self.df_path = 'some'
        self.df_dir = 'some'
        self.original_df = DUMMY_ORIGINAL_DF

        def simplegen(x, y):
            yield "some"
        flexmock(self.tasker, build_image_from_path=simplegen)

    @property
    def source(self):
        return flexmock(
            dockerfile_path='/',
            path='/tmp',
            config=flexmock(image_build_method=None),
        )

    def pull_base_image(self, source_registry, insecure=False):
        pass

    def get_built_image_info(self):
        return {'Id': 'some'}

    def inspect_built_image(self):
        return {INSPECT_ROOTFS: {INSPECT_ROOTFS_LAYERS: ['sha256:diff_id1-oldest',
                                                         'sha256:diff_id2',
                                                         'sha256:diff_id3',
                                                         'sha256:diff_id4-newest']}}

    def ensure_not_built(self):
        pass


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

    def __init__(self, tasker, workflow, watcher, *args, **kwargs):
        super(WatchedMixIn, self).__init__(tasker, workflow,
                                           *args, **kwargs)
        self.watcher = watcher

    def run(self):
        self.watcher.call()


class WatchedBuildStep(object):
    """
    class for buildstep plugins we want to watch.
    """

    def __init__(self, tasker, workflow, watcher, *args, **kwargs):
        super(WatchedBuildStep, self).__init__(tasker, workflow,
                                               *args, **kwargs)
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


def test_workflow_base_images():
    """
    Test workflow for base images
    """

    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreWatched)
    mock_docker()
    fake_builder = MockInsideBuilder(is_base_image=True)
    flexmock(InsideBuilder).new_instances(fake_builder)
    watch_pre = Watcher()
    watch_prepub = Watcher()
    watch_buildstep = Watcher()
    watch_post = Watcher()
    watch_exit = Watcher()
    workflow = DockerBuildWorkflow(source=None,
                                   prebuild_plugins=[{'name': 'pre_watched',
                                                      'args': {
                                                          'watcher': watch_pre
                                                      }}],
                                   buildstep_plugins=[{'name': 'buildstep_watched',
                                                       'args': {
                                                           'watcher': watch_buildstep
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
    assert watch_buildstep.was_called()
    assert watch_post.was_called()
    assert watch_exit.was_called()


def test_workflow_compat(caplog):
    """
    Some of our plugins have changed from being run post-build to
    being run at exit. Let's test what happens when we try running an
    exit plugin as a post-build plugin.
    """
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreWatched)
    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)
    watch_exit = Watcher()
    watch_buildstep = Watcher()

    caplog.clear()

    workflow = DockerBuildWorkflow(source=None,
                                   postbuild_plugins=[{'name': 'store_logs_to_file',
                                                       'args': {
                                                           'watcher': watch_exit
                                                       }}],

                                   buildstep_plugins=[{'name': 'buildstep_watched',
                                                       'args': {
                                                           'watcher': watch_buildstep
                                                       }}],
                                   plugin_files=[this_file])

    workflow.build_docker_image()
    assert watch_exit.was_called()
    for record in caplog.records:
        assert record.levelno != logging.ERROR


def test_set_user_params():
    user_params = {'git_uri': 'test_uri', 'git_ref': 'test_ref', 'git_breanch': 'test_branch'}
    os.environ['USER_PARAMS'] = json.dumps(user_params)

    workflow = DockerBuildWorkflow(source=None)

    for k, v in user_params.items():
        assert workflow.user_params[k] == v


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
    ({'prebuild_plugins': [{'name': 'pre'},
                           {'name': 'pre_watched',
                            'args': {
                                'watcher': Watcher(),
                            }
                            }],
      'buildstep_plugins': [{'name': 'buildstep_watched',
                             'args': {
                                 'watcher': Watcher(),
                             }}]},
     False,  # not fatal
     False,  # no error logged
     ),

    # No 'args' key, buildstep
    ({'buildstep_plugins': [{'name': 'buildstep'},
                            {'name': 'buildstep_watched',
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
                             }],
      'buildstep_plugins': [{'name': 'buildstep_watched',
                             'args': {
                                 'watcher': Watcher(),
                             }}]},
     False,  # not fatal,
     False,  # no error logged
     ),

    # No 'args' key, prepub
    ({'prepublish_plugins': [{'name': 'prepub'},
                             {'name': 'prepub_watched',
                              'args': {
                                  'watcher': Watcher(),
                              }
                              }],
      'buildstep_plugins': [{'name': 'buildstep_watched',
                             'args': {
                                 'watcher': Watcher(),
                             }}]},
     False,  # not fatal,
     False,  # no error logged
     ),

    # No 'args' key, exit
    ({'exit_plugins': [{'name': 'exit'},
                       {'name': 'exit_watched',
                        'args': {
                            'watcher': Watcher(),
                        }
                        }],
      'buildstep_plugins': [{'name': 'buildstep_watched',
                             'args': {
                                 'watcher': Watcher(),
                             }}]},
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

    # No such plugin, buildstep
    ({'buildstep_plugins': [{'name': 'no plugin',
                             'args': {}},
                            {'name': 'buildstep_watched',
                             'args': {
                                 'watcher': Watcher(),
                             }}]},
     False,  # is fatal
     False,  # logs error
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
     True,  # is fatal
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
                            }],
      'buildstep_plugins': [{'name': 'buildstep_watched',
                             'args': {
                                 'watcher': Watcher(),
                             }}]},
     False,  # not fatal
     False,  # does not log error
     ),

    # No such plugin, buildstep, not required
    ({'buildstep_plugins': [{'name': 'no plugin',
                             'args': {},
                             'required': False},
                            {'name': 'buildstep_watched',
                             'args': {
                                 'watcher': Watcher(),
                             }}]},
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
                             }],
      'buildstep_plugins': [{'name': 'buildstep_watched',
                             'args': {
                                 'watcher': Watcher(),
                             }}]},
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
                              }],
      'buildstep_plugins': [{'name': 'buildstep_watched',
                             'args': {
                                 'watcher': Watcher(),
                             }}]},
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
                        }],
      'buildstep_plugins': [{'name': 'buildstep_watched',
                             'args': {
                                 'watcher': Watcher(),
                             }}]},
     False,  # not fatal
     False,  # does not log error
     ),
])
def test_plugin_errors(plugins, should_fail, should_log, caplog):
    """
    Try bad plugin configuration.
    """
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreRaises)
    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)

    caplog.clear()
    workflow = DockerBuildWorkflow(source=None,
                                   plugin_files=[this_file],
                                   **plugins)

    print('===============================================')
    print(plugins)
    print(should_fail)
    print('===============================================')
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
        assert any(record.levelno == logging.ERROR for record in caplog.records)
    else:
        assert all(record.levelno != logging.ERROR for record in caplog.records)


@pytest.mark.parametrize('fail_at', ['pre_raises',
                                     'buildstep_raises',
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
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreRaises)
    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)
    watch_pre = Watcher()
    watch_prepub = Watcher()
    watch_buildstep = Watcher()
    watch_post = Watcher()
    watch_exit = Watcher()
    prebuild_plugins = [{'name': 'pre_watched',
                         'args': {
                             'watcher': watch_pre,
                         }}]
    buildstep_plugins = [{'name': 'buildstep_watched',
                          'args': {
                              'watcher': watch_buildstep,
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
    elif fail_at == 'buildstep_raises':
        buildstep_plugins.insert(0, {'name': fail_at, 'args': {}})
    elif fail_at == 'prepub_raises':
        prepublish_plugins.insert(0, {'name': fail_at, 'args': {}})
    elif fail_at == 'post_raises':
        postbuild_plugins.insert(0, {'name': fail_at, 'args': {}})
    elif fail_at == 'exit_raises' or fail_at == 'exit_raises_allowed':
        exit_plugins.insert(0, {'name': fail_at, 'args': {}})
    else:
        # Typo in the parameter list?
        assert False

    workflow = DockerBuildWorkflow(source=None,
                                   prebuild_plugins=prebuild_plugins,
                                   buildstep_plugins=buildstep_plugins,
                                   prepublish_plugins=prepublish_plugins,
                                   postbuild_plugins=postbuild_plugins,
                                   exit_plugins=exit_plugins,
                                   plugin_files=[this_file])

    # Most failures cause the build process to abort. Unless, it's
    # an exit plugin that's explicitly allowed to fail.
    if fail_at == 'exit_raises_allowed':
        workflow.build_docker_image()
        assert not workflow.plugins_errors
    else:
        with pytest.raises(PluginFailedException):
            workflow.build_docker_image()

        assert fail_at in workflow.plugins_errors

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


def test_workflow_docker_build_error():
    """
    This is a test for what happens when the docker build fails.
    """
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreRaises)
    mock_docker()
    fake_builder = MockInsideBuilder(failed=True)
    flexmock(InsideBuilder).new_instances(fake_builder)
    watch_pre = Watcher()
    watch_buildstep = Watcher(raise_exc=Exception())
    watch_prepub = Watcher()
    watch_post = Watcher()
    watch_exit = Watcher()

    workflow = DockerBuildWorkflow(source=None,
                                   prebuild_plugins=[{'name': 'pre_watched',
                                                      'args': {
                                                          'watcher': watch_pre
                                                      }}],
                                   buildstep_plugins=[{'name': 'buildstep_watched',
                                                       'args': {
                                                           'watcher': watch_buildstep,
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
def test_workflow_docker_build_error_reports(steps_to_fail, step_reported):
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
    mock_docker()
    fake_builder = MockInsideBuilder(failed=True)
    flexmock(InsideBuilder).new_instances(fake_builder)
    watch_pre = construct_watcher('pre')
    watch_buildstep = construct_watcher('buildstep')
    watch_prepub = construct_watcher('prepub')
    watch_post = construct_watcher('post')
    watch_exit = construct_watcher('exit')

    workflow = DockerBuildWorkflow(source=None,
                                   prebuild_plugins=[{'name': 'pre_watched',
                                                      'is_allowed_to_fail': False,
                                                      'args': {
                                                          'watcher': watch_pre
                                                      }}],
                                   buildstep_plugins=[{'name': 'buildstep_watched',
                                                       'is_allowed_to_fail': False,
                                                       'args': {
                                                           'watcher': watch_buildstep,
                                                       }}],
                                   prepublish_plugins=[{'name': 'prepub_watched',
                                                        'is_allowed_to_fail': False,
                                                        'args': {
                                                            'watcher': watch_prepub,
                                                        }}],
                                   postbuild_plugins=[{'name': 'post_watched',
                                                       'is_allowed_to_fail': False,
                                                       'args': {
                                                           'watcher': watch_post
                                                       }}],
                                   exit_plugins=[{'name': 'exit_watched',
                                                  'is_allowed_to_fail': False,
                                                  'args': {
                                                      'watcher': watch_exit
                                                  }}],
                                   plugin_files=[this_file])

    with pytest.raises(Exception) as exc:
        workflow.build_docker_image()
    assert exc_string(step_reported) in str(exc.value)


class ExitUsesSource(ExitWatched):
    key = 'uses_source'

    def run(self):
        assert os.path.exists(self.workflow.source.get_build_file_path()[0])
        WatchedMixIn.run(self)


@requires_internet
def test_source_not_removed_for_exit_plugins():
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreRaises)
    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)
    watch_exit = Watcher()
    watch_buildstep = Watcher()
    workflow = DockerBuildWorkflow(source=None,
                                   exit_plugins=[{'name': 'uses_source',
                                                  'args': {
                                                      'watcher': watch_exit,
                                                  }}],
                                   buildstep_plugins=[{'name': 'buildstep_watched',
                                                       'args': {
                                                           'watcher': watch_buildstep,
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


class ValueBuildStep(object):

    def __init__(self, tasker, workflow, *args, **kwargs):
        super(ValueBuildStep, self).__init__(tasker, workflow, *args, **kwargs)

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
def test_workflow_plugin_results(buildstep_plugin, buildstep_raises):
    """
    Verifies the results of plugins in different phases
    are stored properly.
    It also verifies failed and remote BuildResult is handled properly.
    """

    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreRaises)
    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)

    prebuild_plugins = [{'name': 'pre_build_value'}]
    buildstep_plugins = [{'name': buildstep_plugin}]
    postbuild_plugins = [{'name': 'post_build_value'}]
    prepublish_plugins = [{'name': 'pre_publish_value'}]
    exit_plugins = [{'name': 'exit_value'}]

    workflow = DockerBuildWorkflow(source=None,
                                   prebuild_plugins=prebuild_plugins,
                                   buildstep_plugins=buildstep_plugins,
                                   prepublish_plugins=prepublish_plugins,
                                   postbuild_plugins=postbuild_plugins,
                                   exit_plugins=exit_plugins,
                                   plugin_files=[this_file])

    if buildstep_raises:
        with pytest.raises(PluginFailedException):
            workflow.build_docker_image()
    else:
        workflow.build_docker_image()

    assert workflow.prebuild_results == {'pre_build_value': 'pre_build_value_result'}
    assert isinstance(workflow.buildstep_result[buildstep_plugin], BuildResult)

    if buildstep_raises:
        assert workflow.postbuild_results == {}
        assert workflow.prepub_results == {}
    else:
        assert workflow.postbuild_results == {'post_build_value': 'post_build_value_result'}
        assert workflow.prepub_results == {'pre_publish_value': 'pre_publish_value_result'}

    assert workflow.exit_results == {'exit_value': 'exit_value_result'}


@pytest.mark.parametrize('fail_at', ['pre', 'prepub', 'buildstep', 'post', 'exit'])
def test_cancel_build(fail_at, caplog):
    """
    Verifies that exit plugins are executed when the build is canceled
    """
    # Make the phase we're testing send us SIGTERM
    phase_signal = defaultdict(lambda: None)
    phase_signal[fail_at] = signal.SIGTERM
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreRaises)
    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)
    watch_pre = WatcherWithSignal(phase_signal['pre'])
    watch_prepub = WatcherWithSignal(phase_signal['prepub'])
    watch_buildstep = WatcherWithSignal(phase_signal['buildstep'])
    watch_post = WatcherWithSignal(phase_signal['post'])
    watch_exit = WatcherWithSignal(phase_signal['exit'])

    caplog.clear()

    workflow = DockerBuildWorkflow(source=None,
                                   prebuild_plugins=[{'name': 'pre_watched',
                                                      'args': {
                                                          'watcher': watch_pre
                                                      }}],
                                   prepublish_plugins=[{'name': 'prepub_watched',
                                                        'args': {
                                                            'watcher': watch_prepub,
                                                        }}],
                                   buildstep_plugins=[{'name': 'buildstep_watched',
                                                       'args': {
                                                           'watcher': watch_buildstep
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
    # BaseException repr does not include trailing comma in Python >= 3.7
    # we look for a partial match in log strings for Python < 3.7 compatibility
    expected_entry = (
        "plugin '{}_watched' raised an exception: BuildCanceledException: Build was canceled"
    )
    if fail_at == 'buildstep':
        with pytest.raises(PluginFailedException):
            workflow.build_docker_image()
        assert workflow.build_canceled
        assert any(
            expected_entry.format(fail_at) in record.message
            for record in caplog.records
            if record.levelno == logging.ERROR
        )
    else:
        workflow.build_docker_image()

        if fail_at != 'exit':
            assert workflow.build_canceled
            assert any(
                expected_entry.format(fail_at) in record.message
                for record in caplog.records
                if record.levelno == logging.WARNING
            )
        else:
            assert not workflow.build_canceled

    assert watch_exit.was_called()
    assert watch_pre.was_called()

    if fail_at not in ['pre', 'buildstep']:
        assert watch_prepub.was_called()

    if fail_at not in ['pre', 'prepub', 'buildstep']:
        assert watch_post.was_called()


@pytest.mark.parametrize('has_version', [True, False])
def test_show_version(has_version, caplog):
    """
    Test atomic-reactor print version of osbs-client used to build the build json
    if available
    """
    VERSION = "1.0"
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreRaises)

    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)

    watch_buildstep = Watcher()

    caplog.clear()

    params = {
        'prebuild_plugins': [],
        'buildstep_plugins': [{'name': 'buildstep_watched',
                               'args': {'watcher': watch_buildstep}}],
        'prepublish_plugins': [],
        'postbuild_plugins': [],
        'exit_plugins': [],
        'plugin_files': [this_file],
    }
    if has_version:
        params['client_version'] = VERSION

    workflow = DockerBuildWorkflow(source=None, **params)
    workflow.build_docker_image()
    expected_log_message = "build json was built by osbs-client {}".format(VERSION)
    assert any(
        expected_log_message in record.message
        for record in caplog.records
        if record.levelno == logging.DEBUG
    ) == has_version


def test_layer_sizes():
    flexmock(DockerfileParser, content='df_content')
    this_file = inspect.getfile(PreRaises)
    mock_docker()
    fake_builder = MockInsideBuilder()
    flexmock(InsideBuilder).new_instances(fake_builder)
    watch_exit = Watcher()
    watch_buildstep = Watcher()
    workflow = DockerBuildWorkflow(source=None,
                                   exit_plugins=[{'name': 'uses_source',
                                                  'args': {
                                                      'watcher': watch_exit,
                                                  }}],
                                   buildstep_plugins=[{'name': 'buildstep_watched',
                                                       'args': {
                                                           'watcher': watch_buildstep,
                                                       }}],
                                   plugin_files=[this_file])

    workflow.build_docker_image()

    expected = [
        {'diff_id': u'sha256:diff_id1-oldest', 'size': 4},
        {'diff_id': u'sha256:diff_id2', 'size': 3},
        {'diff_id': u'sha256:diff_id3', 'size': 2},
        {'diff_id': u'sha256:diff_id4-newest', 'size': 1}
    ]

    assert workflow.layer_sizes == expected


@pytest.mark.parametrize('buildstep_plugins, is_orchestrator', [
    (None, False),
    ([], False),
    ([{'name': 'some_name'}], False),
    ([{'name': PLUGIN_BUILD_ORCHESTRATE_KEY}], True),

    ([{'name': 'some_other_name'},
      {'name': PLUGIN_BUILD_ORCHESTRATE_KEY}], True)
])
def test_workflow_is_orchestrator_build(buildstep_plugins, is_orchestrator):
    workflow = DockerBuildWorkflow(source=None,
                                   buildstep_plugins=buildstep_plugins)
    assert workflow.is_orchestrator_build() == is_orchestrator


def test_parent_images_to_str(caplog):
    workflow = DockerBuildWorkflow(source=None)
    workflow.dockerfile_images = DockerfileImages(['fedora:latest', 'bacon'])
    workflow.dockerfile_images['fedora:latest'] = "spam"
    expected_results = {
        "fedora:latest": "spam:latest"
    }
    assert workflow.parent_images_to_str() == expected_results
    assert "None in: base bacon:latest has parent None" in caplog.text


def test_no_base_image(tmpdir):
    workflow = DockerBuildWorkflow(source=None)

    dfp = df_parser(str(tmpdir))
    dfp.content = "# no FROM\nADD spam /eggs"

    workflow._df_path = dfp.dockerfile_path
    with pytest.raises(RuntimeError) as exc:
        workflow.set_df_path(str(tmpdir))
    assert "no base image specified" in str(exc.value)


def test_different_custom_base_images(tmpdir):
    source = {'provider': 'path', 'uri': 'file://' + DOCKERFILE_MULTISTAGE_CUSTOM_BAD_PATH,
              'tmpdir': str(tmpdir)}
    with pytest.raises(NotImplementedError) as exc:
        DockerBuildWorkflow(source=source)
    message = "multiple different custom base images aren't allowed in Dockerfile"
    assert message in str(exc.value)


def test_copy_from_is_blocked(tmpdir):
    """test when user has specified COPY --from=image (instead of builder)"""
    source = {'provider': 'path', 'uri': 'file://' + str(tmpdir), 'tmpdir': str(tmpdir)}

    dfp = df_parser(str(tmpdir))
    dfp.content = dedent("""\
        FROM monty as vikings
        FROM python
        # using a stage name we haven't seen should break:
        COPY --from=notvikings /spam/eggs /bin/eggs
    """)
    with pytest.raises(RuntimeError) as exc_info:
        DockerBuildWorkflow(source=source)
    assert "FROM notvikings AS source" in str(exc_info.value)

    dfp.content = dedent("""\
        FROM monty as vikings
        # using an index we haven't seen should break:
        COPY --from=5 /spam/eggs /bin/eggs
    """)
    with pytest.raises(RuntimeError) as exc_info:
        DockerBuildWorkflow(source=source)
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


class TestPushConf(object):
    def test_new_push_conf(self):
        push_conf = PushConf()
        assert not push_conf.has_some_docker_registry
        assert push_conf.docker_registries == []
        assert push_conf.all_registries == push_conf.docker_registries

    @pytest.mark.parametrize('num_registries', [1, 2])
    def test_add_docker_registry(self, num_registries):
        push_conf = PushConf()
        for n in range(num_registries):
            r = push_conf.add_docker_registry('https://registry{}.example.com'
                                              .format(n),
                                              insecure=False)
            assert isinstance(r, DockerRegistry)
            assert push_conf.has_some_docker_registry
            assert len(push_conf.docker_registries) == n + 1
            assert push_conf.all_registries == push_conf.docker_registries

    @pytest.mark.parametrize('insecure_differs', [False, True])
    def test_readd_docker_registry(self, insecure_differs):
        push_conf = PushConf()
        uri = 'https://registry.example.com'
        first = push_conf.add_docker_registry(uri, insecure=False)
        second = push_conf.add_docker_registry(uri, insecure=insecure_differs)
        assert isinstance(second, DockerRegistry)
        assert first == second
        assert len(push_conf.docker_registries) == 1
        assert push_conf.all_registries == push_conf.docker_registries

    def test_remove_docker_registry(self):
        push_conf = PushConf()
        r = push_conf.add_docker_registry('https://registry.example.com')
        push_conf.remove_docker_registry(r)
        assert not push_conf.has_some_docker_registry
        assert len(push_conf.docker_registries) == 0
        assert push_conf.all_registries == push_conf.docker_registries
