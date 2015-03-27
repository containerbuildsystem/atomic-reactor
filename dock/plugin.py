"""
definition of plugin system

plugins are supposed to be run when image is built and we need to extract some information
"""
import copy
import importlib
import logging
import os
import sys
import traceback
import imp

import dock.plugins
from dock.util import join_img_name_tag


MODULE_EXTENSIONS = ('.py', '.pyc', '.pyo')
logger = logging.getLogger(__name__)


def get_plugin_conf(build_json, plugin_type, plugin_name):
    """
    return dict with configuration of a plugin from provided build json

    :param plugin_type: str, type of plugin (prebuild_plugins, postbuild_plugins, ...)
    :param plugin_name: str, unique name of the plugin
    :return: dict
    """
    logger.debug("getting plugin conf for '%s' with type '%s'",
                 plugin_name, plugin_type)
    plugins_of_a_type = build_json.get(plugin_type, None)
    if plugins_of_a_type is None:
        logger.warning("there are no plugins with type '%s'",
                       plugin_type)
        return
    plugin_conf = [x for x in plugins_of_a_type if x['name'] == plugin_name]
    plugins_num = len(plugin_conf)
    if plugins_num == 1:
        return plugin_conf[0]
    elif plugins_num <= 0:
        logger.warning("there is no configuration for plugin '%s'",
                       plugin_name)
        return
    else:
        logger.error("there is no configuration for plugin '%s'",
                     plugin_name)
        raise RuntimeError("plugin '%s' was specified multiple (%d) times, can't pick one",
                           plugin_name, plugins_num)


class PluginFailedException(Exception):
    """ There was an error during plugin execution """


class Plugin(object):
    """ abstract plugin class """

    # unique plugin identification
    # output of this plugin can be found in results specified with this key,
    # same thing goes for input: use this key for providing input for this plugin
    key = None

    def __init__(self, *args, **kwargs):
        """
        constructor
        """
        self.log = logging.getLogger("dock.plugins." + self.key)
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
        raise NotImplemented()


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
        # imp.findmodule('dock') doesn't work
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
                f_module = imp.load_source("dock.plugins.%s" % module_name, f)
            except (IOError, OSError, ImportError) as ex:
                logger.warning("can't load module '%s': %s", f, repr(ex))
                continue
            for name in dir(f_module):
                binding = getattr(f_module, name, None)
                try:
                    # if you try to compare binding and PostBuildPlugin, python won't match them if you call
                    # this script directly b/c:
                    # ! <class 'plugins.plugin_rpmqa.PostBuildRPMqaPlugin'> <= <class '__main__.PostBuildPlugin'>
                    # but
                    # <class 'plugins.plugin_rpmqa.PostBuildRPMqaPlugin'> <= <class 'dock.plugin.PostBuildPlugin'>
                    is_sub = issubclass(binding, plugin_class)
                except TypeError:
                    is_sub = False
                if binding and is_sub and plugin_class.__name__ != binding.__name__:
                    plugin_classes[binding.key] = binding
        return plugin_classes

    def create_instance_from_plugin(self, plugin_class, plugin_conf):
        """
        create instance from plugin using the plugin class and configuration passed to for it

        input plugins and build plugins initialize differently

        :param plugin_class: plugin class
        :param plugin_conf: dict, configuration for plugin
        :return:
        """
        plugin_instance = plugin_class(**plugin_conf)
        return plugin_instance

    def run(self):
        """
        run all requested plugins
        """
        for plugin_request in self.plugins_conf:
            try:
                plugin_name = plugin_request['name']
            except (TypeError, KeyError):
                logger.error("Invalid plugin request, no key 'name': %s", plugin_request)
                continue
            try:
                plugin_conf = plugin_request.get("args", {})
            except AttributeError:
                logger.error("Invalid plugin request, no key 'args': %s", plugin_request)
                continue
            try:
                plugin_class = self.plugin_classes[plugin_name]
            except KeyError:
                logger.error("No such plugin: '%s', did you set the correct plugin type?", plugin_name)
                continue
            try:
                plugin_can_fail = plugin_request['can_fail']
            except (TypeError, KeyError):
                plugin_can_fail = getattr(plugin_class, "can_fail", True)

            logger.debug("running plugin '%s'", plugin_name)

            plugin_instance = self.create_instance_from_plugin(plugin_class, plugin_conf)

            try:
                plugin_response = plugin_instance.run()
            except Exception as ex:
                msg = "Plugin '%s' raised an exception: '%s'" % (plugin_instance.key, repr(ex))
                logger.warning(msg)
                logger.debug(traceback.format_exc())
                if not plugin_can_fail:
                    raise PluginFailedException(msg)
                plugin_response = msg

            self.plugins_results[plugin_instance.key] = plugin_response
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

    def _translate_special_values(self, dict_to_translate):
        """
        you may want to write plugins for values which are not known before build:
        e.g. id of built image, base image name,... this method will therefore
        translate some reserved values to the runtime values
        """
        translation_dict = {
            'BUILT_IMAGE_ID': self.workflow.builder.image_id,
            'BUILD_DOCKERFILE_PATH' : self.workflow.builder.git_dockerfile_path,
            'BUILD_GIT_PATH' :  self.workflow.builder.git_path,
            'BASE_IMAGE': join_img_name_tag(self.workflow.builder.base_image_name,
                                            self.workflow.builder.base_tag)
        }
        translated_dict = copy.deepcopy(dict_to_translate)
        for key, value in dict_to_translate.items():
            if (not (type(value) is dict)) and value in translation_dict:
                translated_dict[key] = translation_dict[value]
        return translated_dict

    def create_instance_from_plugin(self, plugin_class, plugin_conf):
        translated_conf = self._translate_special_values(plugin_conf)
        logger.info("running plugin instance with args: '%s'", translated_conf)
        plugin_instance = plugin_class(self.dt, self.workflow, **translated_conf)
        return plugin_instance


class PreBuildPlugin(BuildPlugin):
    pass


class PreBuildPluginsRunner(BuildPluginsRunner):

    def __init__(self, dt, workflow, plugins_conf, *args, **kwargs):
        self.plugins_results = workflow.prebuild_results
        super(PreBuildPluginsRunner, self).__init__(dt, workflow, 'PreBuildPlugin', plugins_conf, *args, **kwargs)

class PrePublishPlugin(BuildPlugin):
    pass


class PrePublishPluginsRunner(BuildPluginsRunner):

    def __init__(self, dt, workflow, plugins_conf, *args, **kwargs):
        self.plugins_results = workflow.postbuild_results
        super(PrePublishPluginsRunner, self).__init__(dt, workflow, 'PrePublishPlugin', plugins_conf, *args, **kwargs)


class PostBuildPlugin(BuildPlugin):
    pass


class PostBuildPluginsRunner(BuildPluginsRunner):

    def __init__(self, dt, workflow, plugins_conf, *args, **kwargs):
        self.plugins_results = workflow.postbuild_results
        super(PostBuildPluginsRunner, self).__init__(dt, workflow, 'PostBuildPlugin', plugins_conf, *args, **kwargs)


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
        # key: image, git_uri, prebuildplugins.koji.target, ...
        for key, value in self.substitutions.items():
            key_fragments = key.split(".")
            if len(key_fragments) == 1:
                logger.info("changing value '%s': '%s' -> '%s'",
                            key, build_json[key], value)
                build_json[key] = value
            else:
                try:
                    plugin_type, plugin_name, arg_name = key_fragments
                except ValueError:
                    logger.error("invalid absolute path: it requires exactly three parts: "
                                 "plugin type, plugin name, argument name separated be dot")
                    raise ValueError("invalid absolute path to plugin, it should be "
                                     "plugin_type.plugin_name.argument_name")
                else:
                    plugin_conf = get_plugin_conf(build_json, plugin_type, plugin_name)
                    if plugin_conf is None:
                        logger.warning("no plugin conf found, skipping...")
                    else:
                        logger.info("changing value '%s' of plugin '%s': '%s' -> '%s'",
                                    arg_name, plugin_name, plugin_conf['args'][arg_name], value)
                        plugin_conf['args'][arg_name] = value
        return build_json


class InputPluginsRunner(PluginsRunner):

    def __init__(self, plugins_conf, *args, **kwargs):
        self.plugins_results = {}
        super(InputPluginsRunner, self).__init__('InputPlugin', plugins_conf, *args, **kwargs)
