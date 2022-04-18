"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from typing import Any, List, Union, Dict, Optional

from atomic_reactor.constants import PLUGIN_CHECK_AND_SET_PLATFORMS_KEY
from atomic_reactor.plugin import PluginsRunner
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.config import Configuration, ReactorConfigKeys
from atomic_reactor.util import DockerfileImages


class MockEnv(object):
    """
    Mock environment for unit tests.

    Provides methods for setting up workflow (DockerBuildWorkflow) and runner (PluginsRunner)
    for a specific test scenario.

    Example usage:
    >>> runner = (MockEnv(workflow)
    >>>           .for_plugin('my_plugin')
    >>>           .set_scratch(True)
    >>>           .create_runner())
    >>> runner.run()
    """

    def __init__(self, workflow: DockerBuildWorkflow):
        self.workflow = workflow
        self._plugin_key = None
        self._reactor_config_map = None

    def create_runner(self) -> PluginsRunner:
        """
        Create runner for current plugin (configured using for_plugin())

        :return: PluginsRunner instance (instance of appropriate subclass based on plugin phase)
        """
        return PluginsRunner(
            self.workflow,
            self.workflow.plugins_conf,
            plugins_results=self.workflow.data.plugins_results,
        )

    def for_plugin(self, plugin_key, args=None):
        """
        Set up environment for the specified plugin

        :param plugin_key: str, plugin key
        :param args: dict, optional plugin arguments
        """
        self._plugin_key = plugin_key
        plugins_conf = self.workflow.plugins_conf

        for conf in plugins_conf:
            if conf["name"] == plugin_key:
                raise ValueError(f"This environment already has plugin: {plugin_key}")

        plugins_args = {} if args is None else args
        plugins_conf.append({"name": plugin_key, "args": plugins_args})
        return self

    def set_scratch(self, scratch):
        """
        Set "scratch" user param to specified value
        """
        return self.set_user_params(scratch=scratch)

    def set_isolated(self, isolated):
        """
        Set "isolated" user param to specified value
        """
        return self.set_user_params(isolated=isolated)

    def set_user_params(self, **params):
        """Set user params from keyword arguments."""
        self.workflow.user_params.update(params)
        return self

    def set_check_platforms_result(self, result):
        """Set result of the check_and_set_platforms plugin."""
        return self.set_plugin_result(PLUGIN_CHECK_AND_SET_PLATFORMS_KEY, result)

    def set_plugin_result(self, plugin_key: str, result: Any):
        """
        Set result of the specified plugin (stored in workflow)

        :param plugin_key: str, plugin key
        :param result: any, value to set as plugin result
        """
        self.workflow.data.plugins_results[plugin_key] = result
        return self

    def set_plugin_args(self, args: Dict[str, Any], plugin_key: Optional[str] = None):
        """
        Set plugin arguments (stored in plugins configuration in workflow).

        By default, sets args for the current plugin (configured using for_plugin()).
        Phase and plugin key can be specified to set args for a different plugin.

        If overriding phase and plugin key, the specified plugin must already be present
        in the plugins configuration. Typically, only the current plugin will be present.

        :param args: dict, arguments for plugin
        :param phase: str, optional plugin phase
        :param plugin_key: str, optional plugin key
        """
        plugin_key = plugin_key or self._plugin_key
        plugin = self._get_plugin_conf(plugin_key)
        plugin['args'] = args
        return self

    def set_reactor_config(self, config: Union[Configuration, Dict[str, Any]]):
        """
        Set reactor config map in the workflow

        :param config: If raw config is passed, it will be converted to
            Configuration and the version key is added automatically if omitted.
        :type config: dict[str, any] or Configuration
        """
        if isinstance(config, dict):
            if ReactorConfigKeys.VERSION_KEY not in config:
                config[ReactorConfigKeys.VERSION_KEY] = 1
            config = Configuration(raw_config=config)
        elif isinstance(config, Configuration):
            pass  # Do nothing, use it directly
        else:
            raise TypeError(f"Type {type(config)} of config argument is not supported.")
        self._reactor_config_map = config
        self.workflow.conf = config
        return self

    @property
    def reactor_config(self):
        """
        Get reactor config map (from the ReactorConfigPlugin's workspace)

        If config does not exist, it will be created, i.e. you can do:
        >>> env = MockEnv(workflow)
        >>> env.reactor_config.conf['sources_command'] = 'fedpkg sources'

        :return: ReactorConfig instance
        """
        if not self._reactor_config_map:
            config = Configuration(raw_config={'version': 1})
            self._reactor_config_map = config
            self.workflow.conf = config

        return self._reactor_config_map

    def set_dockerfile_images(self, images: Union[DockerfileImages, List[str]]):
        """Set dockerfile images in the workflow."""
        if not isinstance(images, DockerfileImages):
            images = DockerfileImages(images)
        self.workflow.data.dockerfile_images = images
        return self

    def _get_plugin_conf(self, plugin_key: str) -> Dict[str, Any]:
        for plugin_conf in self.workflow.plugins_conf:
            if plugin_conf['name'] == plugin_key:
                return plugin_conf
        raise ValueError(f'No such plugin: {plugin_key}')
