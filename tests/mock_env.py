"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

from flexmock import flexmock

from atomic_reactor.constants import PLUGIN_BUILD_ORCHESTRATE_KEY
from atomic_reactor.plugin import (PreBuildPluginsRunner,
                                   BuildStepPluginsRunner,
                                   PostBuildPluginsRunner,
                                   PrePublishPluginsRunner,
                                   ExitPluginsRunner)
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfig,
                                                       ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY)

from tests.constants import TEST_IMAGE, MOCK_SOURCE
from tests.stubs import StubSource, StubInsideBuilder


class MockEnv(object):
    """
    Mock environment for unit tests.

    Provides methods for setting up workflow (DockerBuildWorkflow) and runner (PluginsRunner)
    for a specific test scenario.

    Example usage:
    >>> runner = (MockEnv()
    >>>           .for_plugin('prebuild', 'my_plugin')
    >>>           .set_scratch(True)
    >>>           .make_orchestrator()
    >>>           .create_runner(docker_tasker))  # docker_tasker is a fixture
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

    _plugins_for_phase = {phase: phase + '_plugins_conf' for phase in _plugin_phases}

    def __init__(self):
        self.workflow = DockerBuildWorkflow(TEST_IMAGE, source=MOCK_SOURCE)
        self.workflow.source = StubSource()
        self.workflow.builder = StubInsideBuilder().for_workflow(self.workflow)
        self.workflow.builder.tasker = flexmock()

        self._phase = None
        self._plugin_key = None

    def create_runner(self, docker_tasker):
        """
        Create runner for current plugin (configured using for_plugin())

        :param docker_tasker: docker_tasker fixture from conftest

        :return: PluginsRunner instance (instance of appropriate subclass based on plugin phase)
        """
        if self._phase is None:
            raise ValueError('No plugin configured (use for_plugin() to configure one)')
        runner_cls = self._runner_for_phase[self._phase]
        plugins_conf = getattr(self.workflow, self._plugins_for_phase[self._phase])
        return runner_cls(docker_tasker, self.workflow, plugins_conf)

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

        plugins_conf = [self._make_plugin_conf(plugin_key, args)]
        setattr(self.workflow, self._plugins_for_phase[phase], plugins_conf)

        return self

    def set_scratch(self, scratch):
        """
        Set "scratch" user param to specified value
        """
        self.workflow.user_params['scratch'] = scratch
        return self

    def set_isolated(self, isolated):
        """
        Set "isolated" user param to specified value
        """
        self.workflow.user_params['isolated'] = isolated
        return self

    def make_orchestrator(self, orchestrator_args=None):
        """
        Make plugin think it is running in orchestrator

        :param orchestrator_args: dict, optional orchestrate_build plugin arguments
        """
        if self.workflow.buildstep_plugins_conf:
            raise ValueError("Buildstep plugin already configured, cannot make orchestrator")
        self.workflow.buildstep_plugins_conf = [
            self._make_plugin_conf(PLUGIN_BUILD_ORCHESTRATE_KEY, orchestrator_args)
        ]
        return self

    def set_plugin_result(self, phase, plugin_key, result):
        """
        Set result of the specified plugin (stored in workflow)

        :param phase: str, plugin phase
        :param plugin_key: str, plugin key
        :param result: any, value to set as plugin result
        """
        self._validate_phase(phase)
        results = getattr(self.workflow, self._results_for_phase[phase])
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
        Set reactor config map (in the ReactorConfigPlugin's workspace).

        :param config: dict or ReactorConfig, if dict, will be converted to ReactorConfig
        """
        if not isinstance(config, ReactorConfig):
            config = ReactorConfig(config)
        workspace = self._get_reactor_config_workspace()
        workspace[WORKSPACE_CONF_KEY] = config
        return self

    @property
    def reactor_config(self):
        """
        Get reactor config map (from the ReactorConfigPlugin's workspace)

        If config does not exist, it will be created, i.e. you can do:
        >>> env = MockEnv()
        >>> env.reactor_config.conf['sources_command'] = 'fedpkg sources'

        :return: ReactorConfig instance
        """
        workspace = self._get_reactor_config_workspace()
        return workspace.setdefault(WORKSPACE_CONF_KEY, ReactorConfig())

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
        plugins = getattr(self.workflow, self._plugins_for_phase[phase]) or []
        for plugin in plugins:
            if plugin['name'] == plugin_key:
                return plugin
        raise ValueError('No such plugin: {} (for {} phase)'.format(plugin_key, phase))

    def _get_reactor_config_workspace(self):
        return self.workflow.plugin_workspace.setdefault(ReactorConfigPlugin.key, {})
