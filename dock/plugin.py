"""
definition of plugin system

plugins are supposed to be run when image is built and we need to extract some information
"""
import importlib
import os

import dock


MODULE_EXTENSIONS = ('.py', '.pyc', '.pyo')


class PostBuildPlugin(object):
    def __init__(self):
        """ """

    @property
    def key(self):
        """ result of plugin will be under this key in response dict """
        raise NotImplemented()

    @property
    def command(self):
        """ command to run in image """
        raise NotImplemented()


class PostBuildPluginsRunner(object):

    def __init__(self, dt):
        """
        :param dt -- DockerTasker instance
        """
        self.dt = dt
        self.plugin_classes = self.load_plugins()

    def load_plugins(self):
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
        x = importlib.import_module('dock.plugin')
        absolutely_imported_plugin_class = getattr(x, 'PostBuildPlugin')
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

    def run(self, image_id):
        """
        run all postbuild plugins

        :param image_id -- run plugins in this image
        """
        result = {}
        for plugin_class in self.plugin_classes:
            plugin_instance = plugin_class()

            command = plugin_instance.command
            try:
                create_kwargs = plugin_instance.create_container_kwargs
            except AttributeError:
                create_kwargs = {}
            try:
                start_kwargs = plugin_instance.start_container_kwargs
            except AttributeError:
                start_kwargs = {}

            container_id = self.dt.run(image_id, command=plugin_instance.command,
                                       create_kwargs=create_kwargs, start_kwargs=start_kwargs)
            self.dt.wait(container_id)
            plugin_output = self.dt.logs(container_id, stream=False)
            result[plugin_instance.key] = plugin_output
            self.dt.remove_container(container_id)
        return result


if __name__ == '__main__':
    from dock.core import DockerTasker
    dt = DockerTasker()
    r = PostBuildPluginsRunner(dt)
    print r.run('fedora:latest')