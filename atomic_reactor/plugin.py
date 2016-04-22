"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


definition of plugin system

plugins are supposed to be run when image is built and we need to extract some information
"""
import copy
import logging
import os
import traceback
import imp
import datetime

from atomic_reactor.util import process_substitutions

MODULE_EXTENSIONS = ('.py', '.pyc', '.pyo')
logger = logging.getLogger(__name__)


class AutoRebuildCanceledException(Exception):
    """Raised if a plugin cancels autorebuild"""
    def __init__(self, plugin_key, msg):
        self.plugin_key = plugin_key
        self.msg = msg

    def __str__(self):
        return 'plugin %s canceled autorebuild: %s' % (self.plugin_key, self.msg)


class PluginFailedException(Exception):
    """ There was an error during plugin execution """


class Plugin(object):
    """ abstract plugin class """

    # unique plugin identification
    # output of this plugin can be found in results specified with this key,
    # same thing goes for input: use this key for providing input for this plugin
    key = None
    # by default, if plugin fails (raises exc), execution continues
    is_allowed_to_fail = True

    def __init__(self, *args, **kwargs):
        """
        constructor
        """
        self.log = logging.getLogger("atomic_reactor.plugins." + self.key)
        self.args = args
        self.kwargs = kwargs

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
    flavored with DockerTasker and BuildWorkflow instances
    """

    def __init__(self, tasker, workflow, *args, **kwargs):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param args: arguments from user input
        :param kwargs: keyword arguments from user input
        """
        self.tasker = tasker
        self.workflow = workflow
        super(BuildPlugin, self).__init__(*args, **kwargs)


class PluginsRunner(object):

    def __init__(self, plugin_class_name, plugins_conf, *args, **kwargs):
        """
        constructor

        :param plugin_class_name: str, name of plugin class to filter (e.g. 'PreBuildPlugin')
        :param plugins_conf: dict, configuration for plugins
        """
        self.plugins_results = getattr(self, "plugins_results", {})
        self.plugins_conf = plugins_conf or []
        self.plugin_files = kwargs.get("plugin_files", [])
        self.plugin_classes = self.load_plugins(plugin_class_name)

    def load_plugins(self, plugin_class_name):
        """
        load all available plugins
        """
        # imp.findmodule('atomic_reactor') doesn't work
        plugins_dir = os.path.join(os.path.dirname(__file__), 'plugins')
        logger.debug("loading plugins from dir '%s'", plugins_dir)
        files = [os.path.join(plugins_dir, f) \
                 for f in os.listdir(plugins_dir) \
                 if f.endswith(".py")]
        if self.plugin_files:
            logger.debug("loading additional plugins from files '%s'", self.plugin_files)
            files += self.plugin_files
        plugin_class = globals()[plugin_class_name]
        plugin_classes = {}
        for f in files:
            logger.debug("load file '%s'", f)
            module_name = os.path.basename(f).rsplit('.', 1)[0]
            try:
                f_module = imp.load_source(module_name, f)
            except (IOError, OSError, ImportError, SyntaxError) as ex:
                logger.warning("can't load module '%s': %r", f, ex)
                continue
            for name in dir(f_module):
                binding = getattr(f_module, name, None)
                try:
                    # if you try to compare binding and PostBuildPlugin, python won't match them if you call
                    # this script directly b/c:
                    # ! <class 'plugins.plugin_rpmqa.PostBuildRPMqaPlugin'> <= <class '__main__.PostBuildPlugin'>
                    # but
                    # <class 'plugins.plugin_rpmqa.PostBuildRPMqaPlugin'> <= <class 'atomic_reactor.plugin.PostBuildPlugin'>
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

    def run(self, keep_going=False):
        """
        run all requested plugins

        :param keep_going: bool, whether to keep going after unexpected
                                 failure (only used for exit plugins)
        """
        failed_msgs = []
        for plugin_request in self.plugins_conf:
            try:
                plugin_name = plugin_request['name']
            except (TypeError, KeyError):
                self.on_plugin_failed()
                msg = "invalid plugin request, no key 'name': %s" % plugin_request
                logger.error(msg)
                if keep_going:
                    continue

                raise PluginFailedException(msg)

            plugin_conf = plugin_request.get("args", {})
            try:
                plugin_class = self.plugin_classes[plugin_name]
            except KeyError:
                self.on_plugin_failed()
                msg = "no such plugin: '%s', did you set the correct plugin type?" %plugin_name
                logger.error(msg)
                if keep_going:
                    continue

                raise PluginFailedException(msg)
            try:
                plugin_is_allowed_to_fail = plugin_request['is_allowed_to_fail']
            except (TypeError, KeyError):
                plugin_is_allowed_to_fail = getattr(plugin_class, "is_allowed_to_fail", True)

            logger.debug("running plugin '%s'", plugin_name)

            plugin_instance = self.create_instance_from_plugin(plugin_class, plugin_conf)
            start_time = datetime.datetime.now()
            self.save_plugin_timestamp(plugin_instance.key, start_time)

            try:
                plugin_response = plugin_instance.run()
            except AutoRebuildCanceledException as ex:
                # if auto rebuild is canceled, then just reraise
                # NOTE: We need to catch and reraise explicitly, so that the below except clause
                #   doesn't catch this and make PluginFailedException out of it in the end
                #   (calling methods would then need to parse exception message to see if
                #   AutoRebuildCanceledException was raised here)
                raise
            except Exception as ex:
                msg = "plugin '%s' raised an exception: %r" % (plugin_instance.key, ex)
                logger.debug(traceback.format_exc())
                if plugin_is_allowed_to_fail or keep_going:
                    logger.warning(msg)
                    logger.info("error is not fatal, continuing...")
                    if not plugin_is_allowed_to_fail:
                        failed_msgs.append(msg)
                else:
                    self.on_plugin_failed(plugin_instance.key, ex)
                    logger.error(msg)
                    raise PluginFailedException(msg)

                plugin_response = ex

            try:
                finish_time = datetime.datetime.now()
                duration = finish_time - start_time
                seconds = duration.total_seconds()
                logger.debug("plugin '%s' finished in %ds", plugin_name, seconds)
                self.save_plugin_duration(plugin_instance.key, seconds)
            except Exception:
                logger.exception("failed to save plugin duration")

            self.plugins_results[plugin_instance.key] = plugin_response
        if len(failed_msgs) == 1:
            raise PluginFailedException(failed_msgs[0])
        elif len(failed_msgs) > 1:
            raise PluginFailedException("Multiple plugins raised an exception: " + str(failed_msgs))
        return self.plugins_results


class BuildPluginsRunner(PluginsRunner):
    def __init__(self, dt, workflow, plugin_class_name, plugins_conf, *args, **kwargs):
        """
        constructor

        :param dt: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param plugin_class_name: str, name of plugin class to filter (e.g. 'PreBuildPlugin')
        :param plugins_conf: dict, configuration for plugins
        """
        self.dt = dt
        self.workflow = workflow
        super(BuildPluginsRunner, self).__init__(plugin_class_name, plugins_conf, *args, **kwargs)

    def on_plugin_failed(self, plugin=None, exception=None):
        self.workflow.plugin_failed = True
        if plugin and exception:
            self.workflow.plugins_errors[plugin] = repr(exception)

    def save_plugin_timestamp(self, plugin, timestamp):
        self.workflow.plugins_timestamps[plugin] = timestamp.isoformat()

    def save_plugin_duration(self, plugin, duration):
        self.workflow.plugins_durations[plugin] = duration

    def _translate_special_values(self, obj_to_translate):
        """
        you may want to write plugins for values which are not known before build:
        e.g. id of built image, base image name,... this method will therefore
        translate some reserved values to the runtime values
        """
        translation_dict = {
            'BUILT_IMAGE_ID': self.workflow.builder.image_id,
            'BUILD_DOCKERFILE_PATH': self.workflow.builder.source.dockerfile_path,
            'BUILD_SOURCE_PATH':  self.workflow.builder.source.path,
            'BASE_IMAGE': self.workflow.builder.base_image.to_str(),
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

    def create_instance_from_plugin(self, plugin_class, plugin_conf):
        translated_conf = self._translate_special_values(plugin_conf)
        logger.info("running plugin instance with args: '%s'", translated_conf)
        plugin_instance = plugin_class(self.dt, self.workflow, **translated_conf)
        return plugin_instance


class PreBuildPlugin(BuildPlugin):
    pass


class PreBuildPluginsRunner(BuildPluginsRunner):

    def __init__(self, dt, workflow, plugins_conf, *args, **kwargs):
        logger.info("initializing runner of pre-build plugins")
        self.plugins_results = workflow.prebuild_results
        super(PreBuildPluginsRunner, self).__init__(dt, workflow, 'PreBuildPlugin', plugins_conf, *args, **kwargs)

class PrePublishPlugin(BuildPlugin):
    pass


class PrePublishPluginsRunner(BuildPluginsRunner):

    def __init__(self, dt, workflow, plugins_conf, *args, **kwargs):
        logger.info("initializing runner of pre-publish plugins")
        self.plugins_results = workflow.prepub_results
        super(PrePublishPluginsRunner, self).__init__(dt, workflow, 'PrePublishPlugin', plugins_conf, *args, **kwargs)


class PostBuildPlugin(BuildPlugin):
    pass


class PostBuildPluginsRunner(BuildPluginsRunner):

    def __init__(self, dt, workflow, plugins_conf, *args, **kwargs):
        logger.info("initializing runner of post-build plugins")
        self.plugins_results = workflow.postbuild_results
        super(PostBuildPluginsRunner, self).__init__(dt, workflow, 'PostBuildPlugin', plugins_conf, *args, **kwargs)

    def create_instance_from_plugin(self, plugin_class, plugin_conf):
        instance = super(PostBuildPluginsRunner, self).create_instance_from_plugin(plugin_class, plugin_conf)
        if isinstance(instance, ExitPlugin):
            logger.error("running exit plugin '%s' as post-build plugin", plugin_class.key)

        return instance


class ExitPlugin(PostBuildPlugin):
    """
    Plugin base class for plugins which should be run just before
    exit. It is flavored with DockerTasker and DockerBuildWorkflow instances.
    """


class ExitPluginsRunner(BuildPluginsRunner):
    def __init__(self, dt, workflow, plugins_conf, *args, **kwargs):
        logger.info("initializing runner of exit plugins")
        self.plugins_results = workflow.exit_results
        super(ExitPluginsRunner, self).__init__(dt, workflow, 'ExitPlugin',
                                                plugins_conf, *args, **kwargs)


class InputPlugin(Plugin):

    def __init__(self, substitutions=None, **kwargs):
        """
        constructor
        """
        # call parent constructor
        super(InputPlugin, self).__init__(**kwargs)
        self.substitutions = substitutions

    def substitute_configuration(self, build_json):
        """
        replace values of provided build json according to self.substitutions

        path to values can be specified in two ways:

         * single key value for root arguments, e.g. 'image'
         * plugin configuration: you following convention:

             plugin_type.plugin_name.argument_name

           hence

             prebuild_plugins.koji.target

        :param build_json: dict, build json
        :return: dict, substituted build json
        """
        print(self.substitutions)
        process_substitutions(build_json, self.substitutions)
        return build_json

    @classmethod
    def is_autousable(cls):
        """
        Determine if this plugin can run without providing any further user input,
        e.g. if expected default environment variables are defined, if expected default
        files exist etc

        :return: True if this plugin is autousable, False otherwise
        """
        raise NotImplementedError('is_autousable not implemented in {0}'.format(cls))


class InputPluginsRunner(PluginsRunner):
    def __init__(self, plugins_conf, *args, **kwargs):
        super(InputPluginsRunner, self).__init__('InputPlugin', plugins_conf, *args, **kwargs)
        self.plugins_results = {}
        self.autoinput = self.plugins_conf[0]['name'] == 'auto'

    def run(self, keep_going=False):
        """Wrap `PluginsRunner.run()` while implementing the `auto` input behaviour.

        If input plugin name is `auto`, then call `is_autousable` on all input plugins.
        Assuming exactly one of these returns `True`, then use that as input plugin, else raise.
        """
        # implement the "auto" input behavior
        if self.autoinput:
            logger.debug('"auto" input used, determining what input plugin to use.')
            autousable = None
            for clsname, clsobj in self.plugin_classes.items():
                logger.debug('checking if "%s" plugin is autousable ...', clsname)
                if clsobj.is_autousable():
                    if autousable:
                        raise PluginFailedException('More than one usable plugin with "auto" '
                                                    'input: {0}, {1}. Please specify --input '
                                                    'explicitly.'.format(autousable, clsname))
                    else:
                        autousable = clsname
            if not autousable:
                raise PluginFailedException('No autousable input plugin. '
                                            'Please specify --input explicitly')
            logger.debug('using "%s" for input', autousable)
            self.plugins_conf[0]['name'] = autousable

        result = super(InputPluginsRunner, self).run(keep_going=keep_going)

        if self.autoinput:
            result['auto'] = result.pop(autousable)
        return result
