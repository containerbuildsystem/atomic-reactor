"""
Copyright (c) 2017, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from copy import deepcopy
from atomic_reactor.utils.cachito import CachitoAPI
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.constants import (CONTAINER_BUILD_METHODS, CONTAINER_DEFAULT_BUILD_METHOD,
                                      CONTAINER_BUILDAH_BUILD_METHOD)
from atomic_reactor.util import (read_yaml, read_yaml_from_file_path,
                                 get_build_json, DefaultKeyDict)
from osbs.utils import RegistryURI

import logging
import os

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


def get_koji(workflow):
    koji_map = get_value(workflow, 'koji', NO_FALLBACK)

    if 'auth' in koji_map:
        krb_principal = koji_map['auth'].get('krb_principal')
        krb_keytab = koji_map['auth'].get('krb_keytab_path')
        if bool(krb_principal) != bool(krb_keytab):
            raise RuntimeError("specify both koji_principal and koji_keytab or neither")

    return koji_map


def get_pnc(workflow):
    return get_value(workflow, 'pnc', NO_FALLBACK)


def get_koji_session(workflow):
    config = get_koji(workflow)

    from atomic_reactor.utils.koji import create_koji_session

    auth_info = {
        "proxyuser": config['auth'].get('proxyuser'),
        "ssl_certs_dir": config['auth'].get('ssl_certs_dir'),
        "krb_principal": config['auth'].get('krb_principal'),
        "krb_keytab": config['auth'].get('krb_keytab_path')
    }

    use_fast_upload = config.get('use_fast_upload', True)

    return create_koji_session(config['hub_url'], auth_info, use_fast_upload)


def get_koji_path_info(workflow):
    config = get_koji(workflow)
    from koji import PathInfo

    # Make sure koji root_url doesn't end with a slash since the url
    # is used to construct resource urls (e.g. log links)
    root_url = config['root_url'].rstrip('/')
    return PathInfo(topdir=root_url)


def get_odcs(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'odcs', fallback)


def get_odcs_session(workflow):
    config = get_odcs(workflow)
    from atomic_reactor.utils.odcs import ODCSClient

    client_kwargs = {
        'insecure': config.get('insecure', False),
        'timeout': config.get('timeout', None),
    }

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


def get_cachito(workflow):
    return get_value(workflow, 'cachito', NO_FALLBACK)


def get_cachito_session(workflow):
    config = get_cachito(workflow)

    api_kwargs = {
        'insecure': config.get('insecure', False),
        'timeout': config.get('timeout'),
    }

    ssl_certs_dir = config['auth'].get('ssl_certs_dir')
    if ssl_certs_dir:
        cert_path = os.path.join(ssl_certs_dir, 'cert')
        if os.path.exists(cert_path):
            api_kwargs['cert'] = cert_path
        else:
            raise RuntimeError("Cachito ssl_certs_dir doesn't exist")

    return CachitoAPI(config['api_url'], **api_kwargs)


def get_allow_multiple_remote_sources(workflow):
    return get_value(workflow, 'allow_multiple_remote_sources', False)


def get_arrangement_version(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'arrangement_version', fallback)


def get_artifacts_allowed_domains(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'artifacts_allowed_domains', fallback)


def get_yum_repo_allowed_domains(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'yum_repo_allowed_domains', fallback)


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


def get_registries_organization(workflow):
    return get_config(workflow).conf.get('registries_organization')


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
        regdict['expected_media_types'] = registry.get('expected_media_types', [])

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


def _as_source_registry(registry):
    return {
        'uri': RegistryURI(registry['url']),
        'insecure': registry.get('insecure', False),
        'dockercfg_path': registry.get('auth', {}).get('cfg_path', None)
    }


def get_source_registry(workflow, fallback=NO_FALLBACK):
    try:
        source_registry = get_config(workflow).conf['source_registry']
    except KeyError:
        if fallback != NO_FALLBACK:
            return fallback
        raise

    return _as_source_registry(source_registry)


def get_pull_registries(workflow, fallback=NO_FALLBACK):
    """
    Get list of pull_registries from config map, list entries follow the same
    format as the result of get_source_registry()
    """
    try:
        pull_registries = get_config(workflow).conf['pull_registries']
    except KeyError:
        if fallback != NO_FALLBACK:
            return fallback
        raise

    return [_as_source_registry(reg) for reg in pull_registries]


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


def get_buildstep_alias(workflow):
    return get_value(workflow, 'buildstep_alias', {})


def get_flatpak_base_image(workflow, fallback=NO_FALLBACK):
    flatpak = get_value(workflow, 'flatpak', {})
    try:
        return flatpak['base_image']
    except KeyError:
        if fallback != NO_FALLBACK:
            return fallback
        raise


def get_flatpak_metadata(workflow, fallback=NO_FALLBACK):
    flatpak = get_value(workflow, 'flatpak', {})
    try:
        return flatpak['metadata']
    except KeyError:
        if fallback != NO_FALLBACK:
            return fallback
        raise


def get_package_comparison_exceptions(workflow):
    return set(get_config(workflow).conf.get('package_comparison_exceptions', []))


def get_hide_files(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'hide_files', fallback)


def get_skip_koji_check_for_base_image(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'skip_koji_check_for_base_image', fallback)


def get_omps_config(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'omps', fallback)


def get_deep_manifest_list_inspection(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'deep_manifest_list_inspection', fallback)


def get_fail_on_digest_mismatch(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'fail_on_digest_mismatch', fallback)


def get_source_container(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'source_container', fallback)


def get_operator_manifests(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'operator_manifests', fallback)


def get_image_size_limit(workflow):
    config = get_value(workflow, 'image_size_limit', {})
    return {
        'binary_image': config.get('binary_image', 0),
    }


def get_builder_ca_bundle(workflow, fallback=NO_FALLBACK):
    return get_value(workflow, 'builder_ca_bundle', fallback)


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
            self.cluster_configs[platform] = cluster_configs

    def get_enabled_clusters_for_platform(self, platform):
        if platform not in self.cluster_configs:
            return []
        return [conf for conf in self.cluster_configs[platform] if conf.enabled]

    def cluster_defined_for_platform(self, platform):
        return platform in self.cluster_configs

    def get_odcs_config(self):
        """
        Return an odcs config object created from the odcs config configured in
        reactor config

        :return: the object of ODCSConfig. If there is no odcs configured in
            reactor config, None is returned.
        :rtype: :class:`ODCSConfig` or None
        """
        odcs_config = self.conf.get('odcs')
        if odcs_config:
            return ODCSConfig(
                signing_intents=odcs_config['signing_intents'],
                default_signing_intent=odcs_config['default_signing_intent']
            )

    def is_default(self):
        return self.conf == self.DEFAULT_CONFIG


class ODCSConfig(object):
    """
    Configuration for ODCS integration.
    """

    def __init__(self, signing_intents, default_signing_intent):
        self.log = logging.getLogger(self.__class__.__name__)

        self.default_signing_intent = default_signing_intent

        self.signing_intents = []
        # Signing intents are listed in reverse restrictive order in configuration.
        # Since the input signing_intents will be modified by inserting a new
        # key restrictiveness, this deepcopy ensures the original
        # signing_intent dict objects are not modified accidentally.
        for restrictiveness, intent in enumerate(reversed(deepcopy(signing_intents))):
            intent['restrictiveness'] = restrictiveness
            self.signing_intents.append(intent)

        # Verify default_signing_intent is valid
        self.get_signing_intent_by_name(self.default_signing_intent)

    def get_signing_intent_by_name(self, name):
        valid = []
        for entry in self.signing_intents:
            this_name = entry['name']
            if this_name == name:
                return entry

            valid.append(this_name)

        raise ValueError('unknown signing intent name "{}", valid names: {}'
                         .format(name, ', '.join(valid)))

    def get_signing_intent_by_keys(self, keys):
        if isinstance(keys, str):
            keys = keys.split()
        keys = set(keys)
        intents_matching_deprecated_keys = []
        for entry in reversed(self.signing_intents):
            keys_set = set(entry['keys'])
            if (keys and keys_set >= keys) or keys == keys_set:
                return entry

            permissive_keys_set = set(entry['keys'] + entry.get('deprecated_keys', []))
            if keys and permissive_keys_set >= keys:
                intents_matching_deprecated_keys.append(entry)

        if not intents_matching_deprecated_keys:
            raise ValueError('unknown signing intent keys "{}"'.format(keys))

        self.log.warning(
            'signing intent keys "%s" contain deprecated entries in the "%s" signing intent',
            keys,
            intents_matching_deprecated_keys[0]['name']
         )
        return intents_matching_deprecated_keys[0]


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

        :param tasker: ContainerTasker instance
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
        buildstep_aliases = get_buildstep_alias(self.workflow)
        default_image_build_method = get_default_image_build_method(self.workflow)
        source_image_build_method = self.workflow.builder.source.config.image_build_method

        if source_image_build_method in buildstep_aliases:
            source_image_build_method = buildstep_aliases[source_image_build_method]
        if default_image_build_method in buildstep_aliases:
            default_image_build_method = buildstep_aliases[default_image_build_method]

        if (source_image_build_method == CONTAINER_BUILDAH_BUILD_METHOD or
                default_image_build_method == CONTAINER_BUILDAH_BUILD_METHOD):
            raise NotImplementedError('{} method not yet fully implemented'.
                                      format(CONTAINER_BUILDAH_BUILD_METHOD))

        self.workflow.builder.source.config.image_build_method = source_image_build_method
        self.workflow.default_image_build_method = default_image_build_method
        self.workflow.builder.tasker.build_method = (source_image_build_method or
                                                     default_image_build_method)

        # set source registry and organization
        if self.workflow.builder.dockerfile_images:
            source_registry_docker_uri = get_source_registry(self.workflow)['uri'].docker_uri
            organization = get_registries_organization(self.workflow)
            self.workflow.builder.dockerfile_images.set_source_registry(source_registry_docker_uri,
                                                                        organization)
