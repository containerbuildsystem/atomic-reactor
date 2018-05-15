"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from copy import deepcopy
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.constants import CONTAINER_BUILD_METHODS, CONTAINER_DEFAULT_BUILD_METHOD
from atomic_reactor.util import (read_yaml, read_yaml_from_file_path,
                                 get_build_json, DefaultKeyDict)
from osbs.utils import RegistryURI

import os
import six

# Key used to store the config object in the plugin workspace
WORKSPACE_CONF_KEY = 'reactor_config'
NO_FALLBACK = object()


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


def get_value(workflow, name, fallback):
    try:
        # make a deep copy to prevent plugins from changing the value for other plugins
        value = deepcopy(get_config(workflow).conf[name])
        return value
    except KeyError:
        if fallback != NO_FALLBACK:
            return fallback
        raise


def get_koji(workflow, fallback=NO_FALLBACK):
    koji_map = get_value(workflow, 'koji', fallback)

    if 'auth' in koji_map:
        krb_principal = koji_map['auth'].get('krb_principal')
        krb_keytab = koji_map['auth'].get('krb_keytab_path')
        if bool(krb_principal) != bool(krb_keytab):
            raise RuntimeError("specify both koji_principal and koji_keytab or neither")

    return koji_map


def get_koji_session(workflow, fallback):
    config = get_koji(workflow, fallback)

    from atomic_reactor.koji_util import create_koji_session

    auth_info = {
        "proxyuser": config['auth'].get('proxyuser'),
        "ssl_certs_dir": config['auth'].get('ssl_certs_dir'),
        "krb_principal": config['auth'].get('krb_principal'),
        "krb_keytab": config['auth'].get('krb_keytab_path')
    }

    return create_koji_session(config['hub_url'], auth_info)


def get_koji_path_info(workflow, fallback):
    config = get_koji(workflow, fallback)
    from koji import PathInfo

    # Make sure koji root_url doesn't end with a slash since the url
    # is used to construct resource urls (e.g. log links)
    root_url = config['root_url'].rstrip('/')
    return PathInfo(topdir=root_url)


def get_pulp(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'pulp', fallback)


def get_pulp_session(workflow, logger, fallback):
    config = get_pulp(workflow, fallback)

    from atomic_reactor.pulp_util import PulpHandler
    return PulpHandler(workflow, config['name'], logger,
                       pulp_secret_path=config['auth'].get('ssl_certs_dir'),
                       username=config['auth'].get('username'),
                       password=config['auth'].get('password'),
                       dockpulp_loglevel=config.get('loglevel'))


def get_odcs(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'odcs', fallback)


def get_odcs_session(workflow, fallback):
    config = get_odcs(workflow, fallback)
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


def get_smtp(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'smtp', fallback)


def get_smtp_session(workflow, fallback):
    config = get_smtp(workflow, fallback)

    import smtplib
    return smtplib.SMTP(config['host'])


def get_pdc(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'pdc', fallback)


def get_pdc_session(workflow, fallback):
    config = get_pdc(workflow, fallback)

    from pdc_client import PDCClient
    return PDCClient(server=config['api_url'], ssl_verify=not config.get('insecure', False),
                     develop=True)


def get_arrangement_version(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'arrangement_version', fallback)


def get_artifacts_allowed_domains(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'artifacts_allowed_domains', fallback)


def get_image_labels(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'image_labels', fallback)


def get_image_label_info_url_format(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'image_label_info_url_format', fallback)


def get_image_equal_labels(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'image_equal_labels', fallback)


def get_openshift(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'openshift', fallback)


def get_openshift_session(workflow, fallback):
    config = get_openshift(workflow, fallback)
    namespace = get_build_json().get('metadata', {}).get('namespace', None)

    from osbs.api import OSBS
    from osbs.conf import Configuration

    config_kwargs = {
        'verify_ssl': not config.get('insecure', False),
        'namespace': namespace,
        'use_auth': False,
        'conf_file': None,
        'openshift_url': config['url'],
        'build_json_dir': config.get('build_json_dir')
    }

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


def get_group_manifests(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'group_manifests', fallback)


def get_platform_descriptors(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'platform_descriptors', fallback)


def get_prefer_schema1_digest(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'prefer_schema1_digest', fallback)


def get_content_versions(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'content_versions', fallback)


def get_registries(workflow, fallback=NO_FALLBACK):
    try:
        all_registries = get_config(workflow).conf['registries']
    except KeyError:
        if fallback != NO_FALLBACK:
            return fallback
        raise

    registries_cm = {}
    for registry in all_registries:
        reguri = RegistryURI(registry.get('url'))
        regdict = {}
        regdict['version'] = reguri.version
        if registry.get('auth'):
            regdict['secret'] = registry['auth']['cfg_path']
        regdict['insecure'] = registry.get('insecure', False)

        registries_cm[reguri.docker_uri] = regdict

    return registries_cm


def get_docker_registry(workflow, fallback=NO_FALLBACK):
    try:
        all_registries = get_config(workflow).conf['registries']
    except KeyError:
        if fallback != NO_FALLBACK:
            return fallback
        raise

    for registry in all_registries:
        reguri = RegistryURI(registry.get('url'))
        if reguri.version == 'v2':
            regdict = {
                'url': reguri.uri,
                'insecure': registry.get('insecure', False)
            }
            if registry.get('auth'):
                regdict['secret'] = registry['auth']['cfg_path']
            return regdict

    raise RuntimeError("Expected V2 registry but none in REACTOR_CONFIG")


def get_yum_proxy(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'yum_proxy', fallback)


def get_source_registry(workflow, fallback=NO_FALLBACK):
    try:
        source_registry = get_config(workflow).conf['source_registry']
    except KeyError:
        if fallback != NO_FALLBACK:
            return fallback
        raise

    return {
        'uri': RegistryURI(source_registry['url']),
        'insecure': source_registry.get('insecure', False)
    }


def get_sources_command(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'sources_command', fallback)


def get_required_secrets(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'required_secrets', fallback)


def get_worker_token_secrets(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'worker_token_secrets', fallback)


def get_clusters(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'clusters', fallback)


def get_clusters_client_config_path(workflow, fallback=NO_FALLBACK):
    client_config_dir = get_value(workflow, 'clusters_client_config_dir', fallback)
    return os.path.join(client_config_dir, 'osbs.conf')


def get_platform_to_goarch_mapping(workflow,
                                   descriptors_fallback=NO_FALLBACK):
    platform_descriptors = get_platform_descriptors(
        workflow,
        fallback=descriptors_fallback,
    )
    return DefaultKeyDict(
        (descriptor['platform'], descriptor['architecture'])
        for descriptor in platform_descriptors)


def get_goarch_to_platform_mapping(workflow,
                                   descriptors_fallback=NO_FALLBACK):
    platform_descriptors = get_platform_descriptors(
        workflow,
        fallback=descriptors_fallback,
    )
    return DefaultKeyDict(
        (descriptor['architecture'], descriptor['platform'])
        for descriptor in platform_descriptors)


def get_build_image_override(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'build_image_override', fallback)


def get_default_image_build_method(workflow, fallback=CONTAINER_DEFAULT_BUILD_METHOD):
    value = get_value(workflow, 'default_image_build_method', fallback)
    assert value in CONTAINER_BUILD_METHODS, (
        "unknown default_image_build_method '{}' in reactor config; "
        "config schema validation should have caught this."
    ).format(value)
    return value


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
        self.conf = deepcopy(config or self.DEFAULT_CONFIG)

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
        whole_odcs_config = deepcopy(self.conf.get('odcs'))
        odcs_config = None

        if whole_odcs_config:
            odcs_config_kwargs = {}
            odcs_config_kwargs['signing_intents'] = whole_odcs_config['signing_intents']
            odcs_config_kwargs['default_signing_intent'] =\
                whole_odcs_config['default_signing_intent']
            odcs_config = ODCSConfig(**odcs_config_kwargs)
        return odcs_config

    def is_default(self):
        return self.conf == self.DEFAULT_CONFIG


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
        for entry in reversed(self.signing_intents):
            keys_set = set(entry['keys'])
            if (keys and keys_set >= keys) or keys == keys_set:
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
        workspace = self.workflow.plugin_workspace.setdefault(self.key, {})
        workspace[WORKSPACE_CONF_KEY] = reactor_conf

        self.log.info("reading config content %s", reactor_conf.conf)

        # need to stash this on the workflow for access in a place that can't import this module
        self.workflow.default_image_build_method = get_default_image_build_method(self.workflow)
