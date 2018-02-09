"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import read_yaml, read_yaml_from_file_path


import os
import six


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


def get_koji(workflow):
    return get_config(workflow)['koji']


def get_koji_session(workflow):
    config = get_koji(workflow)

    from atomic_reactor.koji_util import create_koji_session
    auth_info = {
        "proxyuser": config['auth'].get('proxyuser'),
        "ssl_certs_dir": config['auth'].get('ssl_certs_dir'),
        "krb_principal": config['auth'].get('krb_principal'),
        "krb_keytab": config['auth'].get('krb_keytab_path')
    }
    return create_koji_session(config['hub_url'], auth_info)


def get_pulp(workflow):
    return get_config(workflow)['pulp']


def get_pulp_session(workflow, logger, loglevel):
    config = get_pulp(workflow)

    from atomic_reactor.pulp_util import PulpHandler
    return PulpHandler(workflow, config['name'], logger,
                       pulp_secret_path=config['auth'].get('ssl_certs_dir'),
                       username=config['auth'].get('username'),
                       password=config['auth'].get('password'),
                       dockpulp_loglevel=loglevel)


def get_odcs(workflow):
    return get_config(workflow)['odcs']


def get_odcs_session(workflow):
    config = get_odcs(workflow)

    from atomic_reactor.odcs_util import ODCSClient

    client_kwargs = {'insecure': config.get('insecure', False)}

    openidc_dir = config['auth'].get('openidc_dir')
    if openidc_dir:
        token_path = os.path.join(openidc_dir, 'token')
        with open(token_path, "r") as f:
            client_kwargs['token'] = f.read().strip()

    ssl_certs_dir = config['auth'].get('ssl_certs_dir')
    if ssl_certs_dir:
        cert_path = os.path.join(ssl_certs_dir, 'cert')
        if os.path.exists(cert_path):
            client_kwargs['cert'] = cert_path
        else:
            raise KeyError("ODCS ssl_certs_dir doesn't exist")

    return ODCSClient(config['api_url'], **client_kwargs)


def get_smtp(workflow):
    return get_config(workflow)['smtp']


def get_smtp_session(workflow):
    config = get_smtp(workflow)

    import smtplib
    return smtplib.SMTP(config['host'])


def get_pdc(workflow):
    return get_config(workflow)['pdc']


def get_pdc_session(workflow):
    config = get_pdc(workflow)

    from pdc_client import PDCClient
    return PDCClient(server=config['api_url'], ssl_verify=not config.get('insecure', False),
                     develop=True)


def get_arrangement_version(workflow):
    return get_config(workflow)['arrangement_version']


def get_artifacts_allowed_domains(workflow):
    return get_config(workflow)['artifacts_allowed_domains']


def get_image_labels(workflow):
    return get_config(workflow)['image_labels']


def get_image_equal_labels(workflow):
    return get_config(workflow)['image_equal_labels']


def get_openshift(workflow):
    return get_config(workflow)['openshift']


def get_openshift_session(workflow, namespace, conf_file=None):
    config = get_openshift(workflow)

    from osbs.api import OSBS
    from osbs.conf import Configuration

    config_kwargs = {
        'config_file': conf_file,
        'openshift_url': config['url'],
        'verify_ssl': not config.get('insecure', False),
        'namespace': namespace,
        'conf_section': None,
        'cli_args': None,
        'use_auth': False,
    }

    try:
        config_kwargs['build_json_dir'] = get_build_json_dir(workflow)
    except KeyError:
        pass

    if config.get('auth'):
        krb_keytab_path = config['auth'].get('krb_keytab_path')
        if krb_keytab_path:
            config_kwargs['kerberos_keytab'] = krb_keytab_path
        krb_principal = config['auth'].get('krb_principal')
        if krb_principal:
            config_kwargs['kerberos_principal'] = krb_principal
        krb_cache_path = config['auth'].get('krb_cache_path')
        if krb_cache_path:
            config_kwargs['kerberos_ccache'] = krb_cache_path
        ssl_certs_dir = config['auth'].get('ssl_certs_dir')
        if ssl_certs_dir:
            config_kwargs['client_cert'] = os.path.join(ssl_certs_dir, 'cert')
            config_kwargs['client_key'] = os.path.join(ssl_certs_dir, 'key')
        config_kwargs['use_auth'] = config['auth'].get('enable', False)

    osbs_conf = Configuration(**config_kwargs)
    return OSBS(osbs_conf, osbs_conf)


def get_group_manifests(workflow):
    return get_config(workflow)['group_manifests']


def get_platform_descriptors(workflow):
    return get_config(workflow)['platform_descriptors']


def get_prefer_schema1_digest(workflow):
    return get_config(workflow)['prefer_schema1_digest']


def get_content_versions(workflow):
    return get_config(workflow)['content_versions']


def get_registries(workflow):
    return get_config(workflow)['registries']


def get_yum_proxy(workflow):
    return get_config(workflow)['yum_proxy']


def get_source_registry(workflow):
    return get_config(workflow)['source_registry']


def get_sources_command(workflow):
    return get_config(workflow)['sources_command']


def get_required_secrets(workflow):
    return get_config(workflow)['required_secrets']


def get_worker_token_secrets(workflow):
    return get_config(workflow)['worker_token_secrets']


def get_build_json_dir(workflow):
    return get_config(workflow)['build_json_dir']


def get_clusters(workflow):
    return get_config(workflow)['clusters']


class ClusterConfig(object):
    """
    Configuration relating to a particular cluster
    """

    def __init__(self, name, max_concurrent_builds, enabled=True, priority=0):
        self.name = str(name)
        self.max_concurrent_builds = int(max_concurrent_builds)
        self.enabled = enabled
        self.priority = priority


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
    ODCS_KEY = 'odcs'


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
            cluster_configs = [ClusterConfig(priority=priority, **cluster)
                               for priority, cluster in enumerate(clusters)]
            self.cluster_configs[platform] = [conf for conf in cluster_configs
                                              if conf.enabled]

    def get_enabled_clusters_for_platform(self, platform):
        return self.cluster_configs.get(platform, [])

    def get_odcs_config(self):
        odcs_config = self.conf.get('odcs')
        if odcs_config:
            odcs_config.pop('auth', None)
            odcs_config.pop('api_url', None)
            odcs_config.pop('insecure', None)
            odcs_config = ODCSConfig(**odcs_config)
        return odcs_config


class ODCSConfig(object):
    """
    Configuration for ODCS integration.
    """

    def __init__(self, signing_intents, default_signing_intent):
        self.default_signing_intent = default_signing_intent

        self.signing_intents = []
        # Signing intents are listed in reverse restrictive order in configuration.
        for restrictiveness, intent in enumerate(reversed(signing_intents)):
            intent['restrictiveness'] = restrictiveness
            self.signing_intents.append(intent)

        # Verify default_signing_intent is valid
        self.get_signing_intent_by_name(self.default_signing_intent)

    def get_signing_intent_by_name(self, name):
        for entry in self.signing_intents:
            if entry['name'] == name:
                return entry

        raise ValueError('unknown signing intent name "{}"'.format(name))

    def get_signing_intent_by_keys(self, keys):
        if isinstance(keys, six.text_type):
            keys = keys.split()
        keys = set(keys)
        for entry in self.signing_intents:
            if set(entry['keys']) == keys:
                return entry

        raise ValueError('unknown signing intent keys "{}"'.format(keys))


class ReactorConfigPlugin(PreBuildPlugin):
    """
    Parse atomic-reactor configuration file
    """

    # Name of this plugin
    key = 'reactor_config'

    # Exceptions from this plugin should fail the build
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, config_path=None, basename='config.yaml'):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param config_path: str, configuration path (directory); default is None
        :param basename: str, filename within directory; default is config.yaml
        """
        # call parent constructor
        super(ReactorConfigPlugin, self).__init__(tasker, workflow)
        self.config_path = config_path
        self.basename = basename
        self.reactor_config_map = os.environ.get('REACTOR_CONFIG', None)


    def run(self):
        """
        Run the plugin

        Parse and validate config.
        Store in workflow workspace for later retrieval.
        """
        if self.reactor_config_map:
            self.log.info("reading config from REACTOR_CONFIG env variable")
            conf = read_yaml(self.reactor_config_map, 'schemas/config.json')
        else:
            config_filename = os.path.join(self.config_path, self.basename)
            self.log.info("reading config from %s", config_filename)
            conf = read_yaml_from_file_path(config_filename, 'schemas/config.json')
        reactor_conf = ReactorConfig(conf)
        workspace = self.workflow.plugin_workspace.get(self.key, {})
        workspace[WORKSPACE_CONF_KEY] = reactor_conf
        self.workflow.plugin_workspace[self.key] = workspace
