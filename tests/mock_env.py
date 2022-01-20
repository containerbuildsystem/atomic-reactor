"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from typing import Iterable, List, Union

from atomic_reactor.constants import (PLUGIN_BUILD_ORCHESTRATE_KEY,
                                      PLUGIN_CHECK_AND_SET_PLATFORMS_KEY)
from atomic_reactor.plugin import (PreBuildPluginsRunner,
                                   BuildStepPluginsRunner,
                                   PostBuildPluginsRunner,
                                   PrePublishPluginsRunner,
                                   ExitPluginsRunner)
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.config import Configuration
from atomic_reactor.util import DockerfileImages


class MockEnv(object):
    """
    Mock environment for unit tests.

    Provides methods for setting up workflow (DockerBuildWorkflow) and runner (PluginsRunner)
    for a specific test scenario.

    Example usage:
    >>> runner = (MockEnv(workflow)
    >>>           .for_plugin('prebuild', 'my_plugin')
    >>>           .set_scratch(True)
    >>>           .make_orchestrator()
    >>>           .create_runner())
    >>> runner.run()
    """

    _plugin_phases = ('prebuild', 'buildstep', 'postbuild', 'prepublish', 'exit')

    _runner_for_phase = {
        'prebuild': PreBuildPluginsRunner,
        'buildstep': BuildStepPluginsRunner,
        'postbuild': PostBuildPluginsRunner,
        'prepublish': PrePublishPluginsRunner,
        'exit': ExitPluginsRunner,
    }

    _results_for_phase = {
        'prebuild': 'prebuild_results',
        'buildstep': 'buildstep_result',
        'postbuild': 'postbuild_results',
        'prepublish': 'prepub_results',
        'exit': 'exit_results',
    }

    def __init__(self, workflow: DockerBuildWorkflow):
        self.workflow = workflow
        self._phase = None
        self._plugin_key = None
        self._reactor_config_map = None

    def create_runner(self):
        """
        Create runner for current plugin (configured using for_plugin())

        :return: PluginsRunner instance (instance of appropriate subclass based on plugin phase)
        """
        if self._phase is None:
            raise ValueError('No plugin configured (use for_plugin() to configure one)')
        runner_cls = self._runner_for_phase[self._phase]
        plugins_conf = getattr(self.workflow.plugins, self._phase)
        return runner_cls(self.workflow, plugins_conf)

    def for_plugin(self, phase, plugin_key, args=None):
        """
        Set up environment for the specified plugin

        :param phase: str, plugin phase (prebuild, buildstep, postbuild, prepublish, exit)
        :param plugin_key: str, plugin key
        :param args: dict, optional plugin arguments
        """
        if self._phase is not None:
            msg = 'Plugin already configured: {} ({} phase)'.format(self._plugin_key, self._phase)
            raise ValueError(msg)

        self._validate_phase(phase)
        self._phase = phase
        self._plugin_key = plugin_key

        plugins = getattr(self.workflow.plugins, phase)
        if plugins:
            raise ValueError(f"This environment already has {phase} plugins: {plugins}")
        plugins.append(self._make_plugin_conf(plugin_key, args))

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

    def set_orchestrator_platforms(self, platforms: Iterable[str]):
        """Set orchestrator platforms and make sure this is an orchestrator environment."""
        try:
            self._get_plugin('buildstep', PLUGIN_BUILD_ORCHESTRATE_KEY)
        except ValueError:
            self.make_orchestrator()
        return self.set_user_params(platforms=list(platforms))

    def make_orchestrator(self, orchestrator_args=None):
        """
        Make plugin think it is running in orchestrator

        :param orchestrator_args: dict, optional orchestrate_build plugin arguments
        """
        if self.workflow.plugins.buildstep:
            raise ValueError("Buildstep plugin already configured, cannot make orchestrator")
        self.workflow.plugins.buildstep.append(
            self._make_plugin_conf(PLUGIN_BUILD_ORCHESTRATE_KEY, orchestrator_args)
        )
        return self

    def set_check_platforms_result(self, result):
        """Set result of the check_and_set_platforms plugin."""
        return self.set_plugin_result("prebuild", PLUGIN_CHECK_AND_SET_PLATFORMS_KEY, result)

    def set_plugin_result(self, phase, plugin_key, result):
        """
        Set result of the specified plugin (stored in workflow)

        :param phase: str, plugin phase
        :param plugin_key: str, plugin key
        :param result: any, value to set as plugin result
        """
        self._validate_phase(phase)
        results = getattr(self.workflow.data, self._results_for_phase[phase])
        results[plugin_key] = result
        return self

    def set_plugin_args(self, args, phase=None, plugin_key=None):
        """
        Set plugin arguments (stored in plugins configuration in workflow).

        By default, sets args for the current plugin (configured using for_plugin()).
        Phase and plugin key can be specified to set args for a different plugin.

        If overriding phase and plugin key, the specified plugin must already be present
        in the plugins configuration. Typically, only the current plugin and the
        orchestrate_build plugin (after make_orchestrator()) will be present.

        :param args: dict, arguments for plugin
        :param phase: str, optional plugin phase
        :param plugin_key: str, optional plugin key
        """
        phase = phase or self._phase
        plugin_key = plugin_key or self._plugin_key
        plugin = self._get_plugin(phase, plugin_key)
        plugin['args'] = args
        return self

    def set_reactor_config(self, config):
        """
        Set reactor config map in the workflow

        :param config: dict or Configuration, if dict, will be converted to Configuration
        """
        if not isinstance(config, Configuration):
            config = Configuration(raw_config=config)
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

    def _validate_phase(self, phase):
        if phase not in self._plugin_phases:
            phases = ', '.join(self._plugin_phases)
            raise ValueError('Invalid plugin phase: {} (valid: {})'.format(phase, phases))

    def _make_plugin_conf(self, name, args):
        plugin = {'name': name}
        if args:
            plugin['args'] = args
        return plugin

    def _get_plugin(self, phase, plugin_key):
        self._validate_phase(phase)
        plugins = getattr(self.workflow.plugins, phase)
        for plugin in plugins:
            if plugin['name'] == plugin_key:
                return plugin
        raise ValueError('No such plugin: {} (for {} phase)'.format(plugin_key, phase))
