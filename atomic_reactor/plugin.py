"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


definition of plugin system

plugins are supposed to be run when image is built and we need to extract some information
"""
from abc import ABC, abstractmethod
import copy
import logging
import os
import sys
import traceback
import imp  # pylint: disable=deprecated-module
import datetime
import inspect
import time
from collections import namedtuple

import atomic_reactor.inner
from atomic_reactor.util import exception_message
from dockerfile_parse import DockerfileParser

MODULE_EXTENSIONS = ('.py', '.pyc', '.pyo')
logger = logging.getLogger(__name__)


class PluginFailedException(Exception):
    """ There was an error during plugin execution """


class BuildCanceledException(Exception):
    """Build was canceled"""


class InappropriateBuildStepError(Exception):
    """Requested build step is not appropriate"""


class Plugin(ABC):
    """ abstract plugin class """

    # by default, if plugin fails (raises exc), execution continues
    is_allowed_to_fail = True

    def __init__(self, *args, **kwargs):
        """
        constructor
        """
        self.log = logging.getLogger("atomic_reactor.plugins." + self.key)
        self.args = args
        self.kwargs = kwargs

    @property
    @abstractmethod
    def key(self) -> str:
        """Unique plugin identification

        Output of this plugin can be found in results specified with this key,
        same thing goes for input: use this key for providing input for this plugin

        In subclass it can be specified just as "key" attribute
        """

        # Implementation notes: because this must be defined in each plugin it's abstract
        # property. For easy implementation it's just instance property not a class
        # property (with py<=3.8 it requires metaprogramming).
        # For py>3.8:
        #  ```
        #  @classmethod
        #  @property
        #  def key(cls) -> str
        #  ```
        return "plugin"

    def __str__(self):
        return "%s" % self.key

    def __repr__(self):
        return "Plugin(key='%s')" % self.key

    def run(self):
        """
        each plugin has to implement this method -- it is used to run the plugin actually

        response from a build plugin is kept and used in json result response like this:

          results[plugin.key] = plugin.run()

        input plugins should emit build json with this method
        """
        raise NotImplementedError()


class BuildPlugin(Plugin):
    """
    abstract plugin class: base for build plugins, it is
    flavored with BuildWorkflow instances
    """

    def __init__(self, workflow, *args, **kwargs):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :param args: arguments from user input
        :param kwargs: keyword arguments from user input
        """
        self.workflow = workflow
        super(BuildPlugin, self).__init__(*args, **kwargs)

    def is_in_orchestrator(self):
        """
        Check if the configuration this plugin is part of is for
        an orchestrator build or a worker build.

        :return: True if orchestrator build, False if worker build
        """
        return self.workflow.is_orchestrator_build()

    @staticmethod
    def args_from_user_params(user_params: dict) -> dict:
        """Get keyword arguments for this plugin based on values in user params.

        Plugin runners will set these automatically for all plugins.
        """
        return {}


class PluginsRunner(object):

    def __init__(self, plugin_class_name, plugins_conf, *args, **kwargs):
        """
        constructor

        :param plugin_class_name: str, name of plugin class to filter (e.g. 'PreBuildPlugin')
        :param plugins_conf: list of dicts, configuration for plugins
        """
        self.plugins_results = getattr(self, "plugins_results", {})
        self.plugins_conf = plugins_conf or []
        self.plugin_files = kwargs.get("plugin_files", [])
        self.plugin_classes = self.load_plugins(plugin_class_name)
        self.available_plugins = self.get_available_plugins()

    def load_plugins(self, plugin_class_name):
        """
        load all available plugins

        :param plugin_class_name: str, name of plugin class (e.g. 'PreBuildPlugin')
        :return: dict, bindings for plugins of the plugin_class_name class
        """
        # imp.findmodule('atomic_reactor') doesn't work
        plugins_dir = os.path.join(os.path.dirname(__file__), 'plugins')
        logger.debug("loading plugins from dir '%s'", plugins_dir)
        files = [os.path.join(plugins_dir, f)
                 for f in os.listdir(plugins_dir)
                 if f.endswith(".py")]
        if self.plugin_files:
            logger.debug("loading additional plugins from files '%s'", self.plugin_files)
            files += self.plugin_files
        plugin_class = globals()[plugin_class_name]
        plugin_classes = {}
        for f in files:
            module_name = os.path.basename(f).rsplit('.', 1)[0]
            # Do not reload plugins
            if module_name in sys.modules:
                f_module = sys.modules[module_name]
            else:
                try:
                    logger.debug("load file '%s'", f)
                    f_module = imp.load_source(module_name, f)
                except (IOError, OSError, ImportError, SyntaxError) as ex:
                    logger.warning("can't load module '%s': %s", f, ex)
                    continue
            for name in dir(f_module):
                binding = getattr(f_module, name, None)
                try:
                    # if you try to compare binding and PostBuildPlugin, python won't match them
                    # if you call this script directly b/c:
                    # ! <class 'plugins.plugin_rpmqa.PostBuildRPMqaPlugin'> <= <class
                    # '__main__.PostBuildPlugin'>
                    # but
                    # <class 'plugins.plugin_rpmqa.PostBuildRPMqaPlugin'> <= <class
                    # 'atomic_reactor.plugin.PostBuildPlugin'>
                    is_sub = issubclass(binding, plugin_class)
                except TypeError:
                    is_sub = False
                if binding and is_sub and plugin_class.__name__ != binding.__name__:
                    plugin_classes[binding.key] = binding
        return plugin_classes

    def create_instance_from_plugin(self, plugin_class, plugin_conf):
        """
        create instance from plugin using the plugin class and configuration passed to for it

        :param plugin_class: plugin class
        :param plugin_conf: dict, configuration for plugin
        :return:
        """
        plugin_instance = plugin_class(**plugin_conf)
        return plugin_instance

    def on_plugin_failed(self, plugin=None, exception=None):
        pass

    def save_plugin_timestamp(self, plugin, timestamp):
        pass

    def save_plugin_duration(self, plugin, duration):
        pass

    def get_available_plugins(self):
        """
        check requested plugins availability
        and handle missing plugins

        :return: list of namedtuples, runnable plugins data
        """
        available_plugins = []
        PluginData = namedtuple('PluginData', 'name, plugin_class, conf, is_allowed_to_fail')
        for plugin_request in self.plugins_conf:
            plugin_name = plugin_request['name']
            try:
                plugin_class = self.plugin_classes[plugin_name]
            except KeyError as e:
                if plugin_request.get('required', True):
                    msg = ("no such plugin: '%s', did you set "
                           "the correct plugin type?") % plugin_name
                    exc = PluginFailedException(msg)
                    self.on_plugin_failed(plugin_name, exc)
                    logger.error(msg)
                    raise exc from e
                else:
                    # This plugin is marked as not being required
                    logger.warning("plugin '%s' requested but not available",
                                   plugin_name)
                    continue
            plugin_is_allowed_to_fail = plugin_request.get('is_allowed_to_fail',
                                                           getattr(plugin_class,
                                                                   "is_allowed_to_fail", True))
            plugin_conf = plugin_request.get("args", {})
            plugin = PluginData(plugin_name,
                                plugin_class,
                                plugin_conf,
                                plugin_is_allowed_to_fail)
            available_plugins.append(plugin)
        return available_plugins

    def run(self, keep_going=False, buildstep_phase=False):
        """
        run all requested plugins

        :param keep_going: bool, whether to keep going after unexpected
                                 failure (only used for exit plugins)
        :param buildstep_phase: bool, when True remaining plugins will
                                not be executed after a plugin completes
                                (only used for build-step plugins)
        """
        failed_msgs = []
        plugin_successful = False
        plugin_response = None
        available_plugins = self.available_plugins
        for plugin in available_plugins:
            plugin_successful = False

            logger.debug("running plugin '%s'", plugin.name)
            start_time = datetime.datetime.now()

            plugin_response = None
            skip_response = False
            try:
                plugin_instance = self.create_instance_from_plugin(plugin.plugin_class,
                                                                   plugin.conf)
                self.save_plugin_timestamp(plugin.plugin_class.key, start_time)
                plugin_response = plugin_instance.run()
                plugin_successful = True
                if buildstep_phase:
                    assert isinstance(plugin_response, atomic_reactor.inner.BuildResult)
                    if plugin_response.is_failed():
                        logger.error("Build step plugin %s failed: %s",
                                     plugin.plugin_class.key,
                                     plugin_response.fail_reason)
                        self.on_plugin_failed(plugin.plugin_class.key,
                                              plugin_response.fail_reason)
                        plugin_successful = False
                        self.plugins_results[plugin.plugin_class.key] = plugin_response
                        break

            except InappropriateBuildStepError:
                logger.debug('Build step %s is not appropriate', plugin.plugin_class.key)
                # don't put None, in results for InappropriateBuildStepError
                skip_response = True
                if not buildstep_phase:
                    raise
            except Exception as ex:
                msg = "plugin '%s' raised an exception: %s" % (plugin.plugin_class.key,
                                                               exception_message(ex))
                logger.debug(traceback.format_exc())
                if not plugin.is_allowed_to_fail:
                    self.on_plugin_failed(plugin.plugin_class.key, ex)

                if plugin.is_allowed_to_fail or keep_going:
                    logger.warning(msg)
                    logger.info("error is not fatal, continuing...")
                    if not plugin.is_allowed_to_fail:
                        failed_msgs.append(msg)
                else:
                    logger.error(msg)
                    raise PluginFailedException(msg) from ex

                plugin_response = ex

            try:
                if start_time:
                    finish_time = datetime.datetime.now()
                    duration = finish_time - start_time
                    seconds = duration.total_seconds()
                    logger.debug("plugin '%s' finished in %ds", plugin.name, seconds)
                    self.save_plugin_duration(plugin.plugin_class.key, seconds)
            except Exception:
                logger.exception("failed to save plugin duration")

            if not skip_response:
                self.plugins_results[plugin.plugin_class.key] = plugin_response

            if plugin_successful and buildstep_phase:
                logger.debug('stopping further execution of plugins '
                             'after first successful plugin')
                break

        if len(failed_msgs) == 1:
            raise PluginFailedException(failed_msgs[0])
        elif len(failed_msgs) > 1:
            raise PluginFailedException("Multiple plugins raised an exception: " +
                                        str(failed_msgs))

        if not plugin_successful and buildstep_phase and not plugin_response:
            self.on_plugin_failed("BuildStepPlugin", "No appropriate build step")
            raise PluginFailedException("No appropriate build step")

        return self.plugins_results


class BuildPluginsRunner(PluginsRunner):
    def __init__(self, workflow, plugin_class_name, plugins_conf, *args, **kwargs):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :param plugin_class_name: str, name of plugin class to filter (e.g. 'PreBuildPlugin')
        :param plugins_conf: list of dicts, configuration for plugins
        """
        self.workflow = workflow
        super(BuildPluginsRunner, self).__init__(plugin_class_name, plugins_conf, *args, **kwargs)

    def on_plugin_failed(self, plugin=None, exception=None):
        self.workflow.data.plugin_failed = True
        if plugin and exception:
            self.workflow.data.plugins_errors[plugin] = str(exception)

    def save_plugin_timestamp(self, plugin, timestamp):
        self.workflow.data.plugins_timestamps[plugin] = timestamp.isoformat()

    def save_plugin_duration(self, plugin, duration):
        self.workflow.data.plugins_durations[plugin] = duration

    def _translate_special_values(self, obj_to_translate):
        """
        you may want to write plugins for values which are not known before build:
        e.g. id of built image, base image name,... this method will therefore
        translate some reserved values to the runtime values
        """
        translation_dict = {
            # OSBS2 TBD
            'BUILT_IMAGE_ID': self.workflow.data.image_id,
            'BUILD_DOCKERFILE_PATH': self.workflow.source.dockerfile_path,
            'BUILD_SOURCE_PATH': self.workflow.source.path,
        }

        if isinstance(obj_to_translate, dict):
            # Recurse into dicts
            translated_dict = copy.deepcopy(obj_to_translate)
            for key, value in obj_to_translate.items():
                translated_dict[key] = self._translate_special_values(value)

            return translated_dict
        elif isinstance(obj_to_translate, list):
            # Iterate over lists
            return [self._translate_special_values(elem)
                    for elem in obj_to_translate]
        else:
            return translation_dict.get(obj_to_translate, obj_to_translate)

    def _remove_unknown_args(self, plugin_class, plugin_conf):
        sig = inspect.getfullargspec(plugin_class.__init__)  # pylint: disable=no-member
        kwargs = sig.varkw

        # Constructor defines **kwargs, it'll take any parameter
        if kwargs:
            return plugin_conf

        args = set(sig.args)
        known_plugin_conf = {}
        for key, value in plugin_conf.items():
            if key not in args:
                logger.warning(
                    '%s constructor does not take %s=%s parameter, ignoring it',
                    plugin_class.__name__, key, value)
                continue
            known_plugin_conf[key] = value

        return known_plugin_conf

    def create_instance_from_plugin(self, plugin_class, plugin_conf):
        plugin_conf = self._remove_unknown_args(plugin_class, plugin_conf)
        plugin_conf.update(plugin_class.args_from_user_params(self.workflow.user_params))
        plugin_conf = self._translate_special_values(plugin_conf)
        logger.info("running plugin instance with args: '%s'", plugin_conf)
        plugin_instance = plugin_class(self.workflow, **plugin_conf)
        return plugin_instance


class PreBuildPlugin(BuildPlugin):
    pass


class PreBuildPluginsRunner(BuildPluginsRunner):

    def __init__(self, workflow, plugins_conf, *args, **kwargs):
        logger.info("initializing runner of pre-build plugins")
        self.plugins_results = workflow.data.prebuild_results
        super(PreBuildPluginsRunner, self).__init__(workflow, 'PreBuildPlugin', plugins_conf,
                                                    *args, **kwargs)


class BuildStepPlugin(BuildPlugin):
    pass


class BuildStepPluginsRunner(BuildPluginsRunner):

    def __init__(self, workflow, plugin_conf, *args, **kwargs):
        logger.info("initializing runner of build-step plugin")
        self.plugins_results = workflow.data.buildstep_result

        if plugin_conf:
            # any non existing buildstep plugin must be skipped without error
            for plugin in plugin_conf:
                plugin['required'] = False
                plugin['is_allowed_to_fail'] = False

        super(BuildStepPluginsRunner, self).__init__(
            workflow, 'BuildStepPlugin', plugin_conf, *args, **kwargs)

    def run(self, keep_going=False, buildstep_phase=True):
        logger.info('building image %r inside current environment',
                    self.workflow.image)
        if self.workflow.df_path:
            logger.debug('using dockerfile:\n%s',
                         DockerfileParser(self.workflow.df_path).content)
        else:
            logger.debug("No Dockerfile path has been specified")

        plugins_results = super(BuildStepPluginsRunner, self).run(
            keep_going=keep_going, buildstep_phase=buildstep_phase
        )
        return list(plugins_results.values())[0]


class PrePublishPlugin(BuildPlugin):
    pass


class PrePublishPluginsRunner(BuildPluginsRunner):

    def __init__(self, workflow, plugins_conf, *args, **kwargs):
        logger.info("initializing runner of pre-publish plugins")
        self.plugins_results = workflow.data.prepub_results
        super(PrePublishPluginsRunner, self).__init__(workflow, 'PrePublishPlugin',
                                                      plugins_conf, *args, **kwargs)


class PostBuildPlugin(BuildPlugin):
    pass


class PostBuildPluginsRunner(BuildPluginsRunner):

    def __init__(self, workflow, plugins_conf, *args, **kwargs):
        logger.info("initializing runner of post-build plugins")
        self.plugins_results = workflow.data.postbuild_results
        super(PostBuildPluginsRunner, self).__init__(workflow, 'PostBuildPlugin',
                                                     plugins_conf, *args, **kwargs)

    def create_instance_from_plugin(self, plugin_class, plugin_conf):
        instance = super(PostBuildPluginsRunner, self).create_instance_from_plugin(plugin_class,
                                                                                   plugin_conf)

        return instance


class ExitPlugin(PostBuildPlugin):
    """
    Plugin base class for plugins which should be run just before
    exit. It is flavored with DockerBuildWorkflow instances.
    """


class ExitPluginsRunner(BuildPluginsRunner):
    def __init__(self, workflow, plugins_conf, *args, **kwargs):
        logger.info("initializing runner of exit plugins")
        self.plugins_results = workflow.data.exit_results
        super(ExitPluginsRunner, self).__init__(workflow, 'ExitPlugin',
                                                plugins_conf, *args, **kwargs)


# Built-in plugins
class PreBuildSleepPlugin(PreBuildPlugin):
    """
    Sleep for a specified number of seconds.

    This plugin is only intended to be used for debugging.
    """

    key = 'pre_sleep'

    def __init__(self, workflow, seconds=60):
        self.seconds = seconds

    def run(self):
        time.sleep(self.seconds)
