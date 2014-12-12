"""
definition of plugin system

plugins are supposed to be run when image is built and we need to extract some information
"""
import copy
import importlib
import logging
import os

import dock.plugins
from dock.util import join_img_name_tag


MODULE_EXTENSIONS = ('.py', '.pyc', '.pyo')
logger = logging.getLogger(__name__)


class Plugin(object):
    """ abstract plugin class """

    # unique plugin identification
    # output of this plugin can be found in results specified with this key,
    # same thing goes for input: use this key for providing input for this plugin
    key = None

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
        self.log = logging.getLogger("dock.plugins." + self.key)

    def run(self):
        """
        each plugin has to implement this method -- it is used to run the plugin actually

        response from plugin is kept and used in json result response like this:

          results[plugin.key] = plugin.run()

        """
        raise NotImplemented()


class PluginsRunner(object):

    def __init__(self, dt, workflow, plugin_class_name, plugins_conf):
        """
        constructor

        :param dt: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param plugins_conf: dict, configuration for plugins
        """
        self.dt = dt
        self.workflow = workflow
        self.plugins_conf = plugins_conf or {}
        self.plugin_classes = self.load_plugins(plugin_class_name)

    def load_plugins(self, plugin_class_name):
        """
        load plugins
        """
        # imp.findmodule('dock') doesn't work
        file = dock.plugins.__file__
        plugins_dir = os.path.dirname(file)
        plugins = set(['dock.plugins.' + os.path.splitext(module)[0]
                       for module in os.listdir(plugins_dir)
                       if module.endswith(MODULE_EXTENSIONS) and
                       not module.startswith('__init__.py')])
        this_module = importlib.import_module('dock.plugin')
        absolutely_imported_plugin_class = getattr(this_module, plugin_class_name)
        plugin_classes = []
        for plugin_name in plugins:
            plugin = importlib.import_module(plugin_name)
            for name in dir(plugin):
                binding = getattr(plugin, name, None)
                try:
                    # if you try to compare binding and PostBuildPlugin, python won't match them if you call
                    # this script directly b/c:
                    # ! <class 'plugins.plugin_rpmqa.PostBuildRPMqaPlugin'> <= <class '__main__.PostBuildPlugin'>
                    # but
                    # <class 'plugins.plugin_rpmqa.PostBuildRPMqaPlugin'> <= <class 'dock.plugin.PostBuildPlugin'>
                    is_sub = issubclass(binding, absolutely_imported_plugin_class)
                except TypeError:
                    is_sub = False
                if binding and is_sub and absolutely_imported_plugin_class.__name__ != binding.__name__:
                    plugin_classes.append(binding)
        return plugin_classes

    def _translate_special_values(self, dict_to_translate):
        """
        you may want to write plugins for values which are not known before build:
        e.g. id of built image, base image name,... this method will therefore
        translate some reserved values to the runtime values
        """
        translation_dict = {
            'BUILT_IMAGE_ID': self.workflow.builder.image_id,
            'BASE_IMAGE': join_img_name_tag(self.workflow.builder.base_image_name,
                                            self.workflow.builder.base_image_tag)
        }
        translated_dict = copy.deepcopy(dict_to_translate)
        for key, value in dict_to_translate.items():
            if value in translation_dict:
                translated_dict[key] = translation_dict[value]
        return translated_dict

    def run(self):
        """
        run all requested plugins
        """
        result = {}
        for plugin_class in self.plugin_classes:
            plugin_name = plugin_class.key
            if plugin_name not in self.plugins_conf:
                logger.debug("skipping plugin '%s', it is not requested", plugin_name)
                continue
            plugin_conf = self.plugins_conf[plugin_name]
            translated_conf = self._translate_special_values(plugin_conf)
            plugin_instance = plugin_class(self.dt, self.workflow, **translated_conf)

            try:
                plugin_response = plugin_instance.run()
            except Exception as ex:
                msg = "Plugin '%s' raised an exception: '%s'" % (plugin_instance.key, repr(ex))
                logger.error(msg)
                plugin_response = msg

            result[plugin_instance.key] = plugin_response
        return result


class PreBuildPlugin(Plugin):
    pass


class PreBuildPluginsRunner(PluginsRunner):

    def __init__(self, dt, workflow, plugins_conf, *args, **kwargs):
        super(PreBuildPluginsRunner, self).__init__(dt, workflow, 'PreBuildPlugin', plugins_conf, *args, **kwargs)


class PostBuildPlugin(Plugin):
    pass


class PostBuildPluginsRunner(PluginsRunner):

    def __init__(self, dt, workflow, plugins_conf, *args, **kwargs):
        super(PostBuildPluginsRunner, self).__init__(dt, workflow, 'PostBuildPlugin', plugins_conf, *args, **kwargs)
