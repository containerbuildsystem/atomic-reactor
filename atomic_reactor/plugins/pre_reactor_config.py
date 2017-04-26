"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import read_yaml


import os


# Key used to store the config object in the plugin workspace
WORKSPACE_CONF_KEY = 'reactor_config'


def get_config(workflow):
    """
    Obtain configuration object
    Does not fail

    :return: ReactorConfig instance
    """
    try:
        workspace = workflow.plugin_workspace[ReactorConfigPlugin.key]
        return workspace[WORKSPACE_CONF_KEY]
    except KeyError:
        # The plugin did not run or was not successful: use defaults
        conf = ReactorConfig()
        workspace = workflow.plugin_workspace.get(ReactorConfigPlugin.key, {})
        workspace[WORKSPACE_CONF_KEY] = conf
        workflow.plugin_workspace[ReactorConfigPlugin.key] = workspace
        return conf


class ClusterConfig(object):
    """
    Configuration relating to a particular cluster
    """

    def __init__(self, name, max_concurrent_builds, enabled=True):
        self.name = str(name)
        self.max_concurrent_builds = int(max_concurrent_builds)
        self.enabled = enabled


class ReactorConfigKeys(object):
    """
    Symbolic names to use for the key names in the configuration file

    Use the symbols defined in this class to fetch key values from
    the configuration file rather than using string literals. This
    way if you mis-spell one it will cause an exception to be raised
    rather than the key seeming not to be present in the config file.

    At top level:
    - VERSION_KEY: this is the version of the config file schema
    - CLUSTERS_KEY: this holds details about clusters, by platform
    """

    VERSION_KEY = 'version'
    CLUSTERS_KEY = 'clusters'

class ReactorConfig(object):
    """
    Class to parse the atomic-reactor configuration file
    """

    DEFAULT_CONFIG = {ReactorConfigKeys.VERSION_KEY: 1}

    def __init__(self, config=None):
        self.conf = config or self.DEFAULT_CONFIG

        version = self.conf[ReactorConfigKeys.VERSION_KEY]
        if version != 1:
            raise ValueError("version %r unknown" % version)

        # Prepare cluster configurations
        self.cluster_configs = {}
        for platform, clusters in self.conf.get(ReactorConfigKeys.CLUSTERS_KEY,
                                                {}).items():
            cluster_configs = [ClusterConfig(**cluster) for cluster in clusters]
            self.cluster_configs[platform] = [conf for conf in cluster_configs
                                              if conf.enabled]

    def get_enabled_clusters_for_platform(self, platform):
        return self.cluster_configs.get(platform, [])


class ReactorConfigPlugin(PreBuildPlugin):
    """
    Parse atomic-reactor configuration file
    """

    # Name of this plugin
    key = 'reactor_config'

    # Exceptions from this plugin should fail the build
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, config_path, basename='config.yaml'):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param config_path: str, configuration path (directory)
        :param basename: str, filename within directory; default is config.yaml
        """
        # call parent constructor
        super(ReactorConfigPlugin, self).__init__(tasker, workflow)
        self.config_path = config_path
        self.basename = basename

    def run(self):
        """
        Run the plugin

        Parse and validate config.
        Store in workflow workspace for later retrieval.
        """

        config_filename = os.path.join(self.config_path, self.basename)
        self.log.info("reading config from %s", config_filename)
        conf = read_yaml(config_filename, 'schemas/config.json')
        reactor_conf = ReactorConfig(conf)
        workspace = self.workflow.plugin_workspace.get(self.key, {})
        workspace[WORKSPACE_CONF_KEY] = reactor_conf
        self.workflow.plugin_workspace[self.key] = workspace
