"""
Copyright (c) 2021-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from copy import deepcopy
from atomic_reactor.utils.cachito import CachitoAPI
from atomic_reactor.constants import REACTOR_CONFIG_ENV_NAME
from atomic_reactor.util import (
    read_yaml,
    read_yaml_from_file_path,
    DefaultKeyDict,
    DockerfileImages,
)
from osbs.utils import RegistryURI

import logging
import os

NO_FALLBACK = object()

logger = logging.getLogger(__name__)


def get_koji_session(config):
    from atomic_reactor.utils.koji import create_koji_session

    auth_info = {
        "proxyuser": config.koji['auth'].get('proxyuser'),
        "ssl_certs_dir": config.koji['auth'].get('ssl_certs_dir'),
        "krb_principal": config.koji['auth'].get('krb_principal'),
        "krb_keytab": config.koji['auth'].get('krb_keytab_path')
    }

    use_fast_upload = config.koji.get('use_fast_upload', True)

    return create_koji_session(config.koji['hub_url'], auth_info, use_fast_upload)


def get_odcs_session(config):
    from atomic_reactor.utils.odcs import ODCSClient

    client_kwargs = {
        'insecure': config.odcs.get('insecure', False),
        'timeout': config.odcs.get('timeout', None),
    }

    openidc_dir = config.odcs['auth'].get('openidc_dir')
    if openidc_dir:
        token_path = os.path.join(openidc_dir, 'token')
        with open(token_path, "r") as f:
            client_kwargs['token'] = f.read().strip()

    ssl_certs_dir = config.odcs['auth'].get('ssl_certs_dir')
    if ssl_certs_dir:
        cert_path = os.path.join(ssl_certs_dir, 'cert')
        if os.path.exists(cert_path):
            client_kwargs['cert'] = cert_path
        else:
            raise KeyError("ODCS ssl_certs_dir doesn't exist")

    return ODCSClient(config.odcs['api_url'], **client_kwargs)


def get_smtp_session(config):
    import smtplib
    return smtplib.SMTP(config.smtp['host'])


def get_cachito_session(config):
    api_kwargs = {
        'insecure': config.cachito.get('insecure', False),
        'timeout': config.cachito.get('timeout'),
    }

    ssl_certs_dir = config.cachito['auth'].get('ssl_certs_dir')
    if ssl_certs_dir:
        cert_path = os.path.join(ssl_certs_dir, 'cert')
        if os.path.exists(cert_path):
            api_kwargs['cert'] = cert_path
        else:
            raise RuntimeError("Cachito ssl_certs_dir doesn't exist")

    return CachitoAPI(config.cachito['api_url'], **api_kwargs)


def get_openshift_session(config, namespace):
    from osbs.api import OSBS
    from osbs.conf import Configuration

    config_kwargs = {
        'verify_ssl': not config.openshift.get('insecure', False),
        'namespace': namespace,
        'use_auth': False,
        'conf_file': None,
        'openshift_url': config.openshift['url'],
        'build_json_dir': config.openshift.get('build_json_dir')
    }

    if config.openshift.get('auth'):
        krb_keytab_path = config.openshift['auth'].get('krb_keytab_path')
        if krb_keytab_path:
            config_kwargs['kerberos_keytab'] = krb_keytab_path
        krb_principal = config.openshift['auth'].get('krb_principal')
        if krb_principal:
            config_kwargs['kerberos_principal'] = krb_principal
        krb_cache_path = config.openshift['auth'].get('krb_cache_path')
        if krb_cache_path:
            config_kwargs['kerberos_ccache'] = krb_cache_path
        ssl_certs_dir = config.openshift['auth'].get('ssl_certs_dir')
        if ssl_certs_dir:
            config_kwargs['client_cert'] = os.path.join(ssl_certs_dir, 'cert')
            config_kwargs['client_key'] = os.path.join(ssl_certs_dir, 'key')
        config_kwargs['use_auth'] = config.openshift['auth'].get('enable', False)

    osbs_conf = Configuration(**config_kwargs)
    return OSBS(osbs_conf)


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
    """
    VERSION_KEY = 'version'
    CLUSTERS_KEY = 'clusters'
    ODCS_KEY = 'odcs'
    KOJI_KEY = 'koji'
    PNC_KEY = 'pnc'
    SMTP_KEY = 'smtp'
    CACHITO_KEY = 'cachito'
    ALLOW_MULTIPLE_REMOTE_SOURCES_KEY = 'allow_multiple_remote_sources'
    ARTIFACTS_ALLOWED_DOMAINS_KEY = 'artifacts_allowed_domains'
    YUM_REPO_ALLOWED_DOMAINS_KEY = 'yum_repo_allowed_domains'
    IMAGE_LABELS_KEY = 'image_labels'
    IMAGE_LABEL_INFO_URL_FORMAT_KEY = 'image_label_info_url_format'
    IMAGE_EQUAL_LABELS_KEY = 'image_equal_labels'
    OPENSHIFT_KEY = 'openshift'
    GROUP_MANIFESTS_KEY = 'group_manifests'
    PLATFORM_DESCRIPTORS_KEY = 'platform_descriptors'
    PREFER_SCHEMA1_DIGEST_KEY = 'prefer_schema1_digest'
    CONTENT_VERSIONS_KEY = 'content_versions'
    REGISTRIES_ORGANIZATION_KEY = 'registries_organization'
    REGISTRIES_KEY = 'registries'
    YUM_PROXY_KEY = 'yum_proxy'
    SOURCE_REGISTRY_KEY = 'source_registry'
    PULL_REGISTRIES_KEY = 'pull_registries'
    SOURCES_COMMAND_KEY = 'sources_command'
    LIST_RPMS_FROM_SCRATCH_KEY = 'list_rpms_from_scratch'
    REQUIRED_SECRETS_KEY = 'required_secrets'
    WORKER_TOKEN_SECRETS_KEY = 'worker_token_secrets'
    BUILD_IMAGE_OVERRIDE_KEY = 'build_image_override'
    FLATPAK_KEY = 'flatpak'
    PACKAGE_COMPARISON_EXCEPTIONS_KEY = 'package_comparison_exceptions'
    HIDE_FILES_KEY = 'hide_files'
    SKIP_KOJI_CHECK_FOR_BASE_IMAGE_KEY = 'skip_koji_check_for_base_image'
    DEEP_MANIFEST_LIST_INSPECTION_KEY = 'deep_manifest_list_inspection'
    FAIL_ON_DIGEST_MISMATCH_KEY = 'fail_on_digest_mismatch'
    SOURCE_CONTAINER_KEY = 'source_container'
    OPERATOR_MANIFESTS_KEY = 'operator_manifests'
    IMAGE_SIZE_LIMIT_KEY = 'image_size_limit'
    BUILDER_CA_BUNDLE_KEY = 'builder_ca_bundle'


class ODCSConfig(object):
    """
    Configuration for ODCS integration.
    """
    def __init__(self, signing_intents, default_signing_intent):
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

        logger.warning(
            'signing intent keys "%s" contain deprecated entries in the "%s" signing intent',
            keys,
            intents_matching_deprecated_keys[0]['name']
         )
        return intents_matching_deprecated_keys[0]


class Configuration(object):
    """
    Class to parse the atomic-reactor configuration file
    """
    DEFAULT_CONFIG = {ReactorConfigKeys.VERSION_KEY: 1}

    def __init__(self, config_path=None, env_name=REACTOR_CONFIG_ENV_NAME, raw_config=None):
        self.conf = deepcopy(self.DEFAULT_CONFIG)
        reactor_config_from_env = os.environ.get(env_name, None)

        if raw_config:
            logger.info("reading config from raw_config kwarg")
            self.conf = deepcopy(raw_config)

        elif reactor_config_from_env:
            logger.info("reading config from %s env variable", env_name)
            self.conf = read_yaml(reactor_config_from_env, 'schemas/config.json')

        elif config_path and os.path.exists(config_path):
            logger.info("reading config from %s", config_path)
            self.conf = read_yaml_from_file_path(config_path, 'schemas/config.json')

        else:
            logger.info("using default config: %s", self.DEFAULT_CONFIG)

        version = self.conf[ReactorConfigKeys.VERSION_KEY]
        if version != 1:
            raise ValueError("version %r unknown" % version)

        logger.info("reading config content %s", self.conf)

    def update_dockerfile_images_from_config(self, dockerfile_images: DockerfileImages) -> None:
        """
        Set source registry and organization in dockerfile images.
        """
        # only update if there are any actual images (not just 'scratch')
        if dockerfile_images:
            source_registry_docker_uri = self.source_registry['uri'].docker_uri
            organization = self.registries_organization
            dockerfile_images.set_source_registry(source_registry_docker_uri, organization)

    def _get_cluster_configuration(self):
        all_cluster_configs = {}
        for platform, clusters in self.clusters.items():
            cluster_configs = [ClusterConfig(priority=priority, **cluster)
                               for priority, cluster in enumerate(clusters)]
            all_cluster_configs[platform] = cluster_configs
        return all_cluster_configs

    def get_enabled_clusters_for_platform(self, platform):
        cluster_configs = self._get_cluster_configuration()
        if platform not in cluster_configs:
            return []
        return [conf for conf in cluster_configs[platform] if conf.enabled]

    def cluster_defined_for_platform(self, platform):
        cluster_configs = self._get_cluster_configuration()
        return platform in cluster_configs

    @property
    def odcs_config(self):
        """
        Return an odcs config object created from the odcs config configured in
        reactor config

        :return: the object of ODCSConfig. If there is no odcs configured in
            reactor config, None is returned.
        :rtype: :class:`ODCSConfig` or None
        """
        if self.odcs:
            return ODCSConfig(
                signing_intents=self.odcs['signing_intents'],
                default_signing_intent=self.odcs['default_signing_intent']
            )

    def is_default(self):
        return self.conf == self.DEFAULT_CONFIG

    def _get_value(self, name, fallback=NO_FALLBACK):
        try:
            # make a deep copy to prevent plugins from changing the value for other plugins
            value = deepcopy(self.conf[name])
            return value
        except KeyError:
            if fallback != NO_FALLBACK:
                return fallback
            raise

    @property
    def koji(self):
        koji_map = self._get_value(ReactorConfigKeys.KOJI_KEY)

        if 'auth' in koji_map:
            krb_principal = koji_map['auth'].get('krb_principal')
            krb_keytab = koji_map['auth'].get('krb_keytab_path')
            if bool(krb_principal) != bool(krb_keytab):
                raise RuntimeError("specify both koji_principal and koji_keytab or neither")

        return koji_map

    @property
    def koji_path_info(self):
        from koji import PathInfo

        # Make sure koji root_url doesn't end with a slash since the url
        # is used to construct resource urls (e.g. log links)
        root_url = self.koji['root_url'].rstrip('/')
        return PathInfo(topdir=root_url)

    @property
    def pnc(self):
        return self._get_value(ReactorConfigKeys.PNC_KEY, fallback={})

    @property
    def odcs(self):
        return self._get_value(ReactorConfigKeys.ODCS_KEY, fallback={})

    @property
    def smtp(self):
        return self._get_value(ReactorConfigKeys.SMTP_KEY, fallback={})

    @property
    def cachito(self):
        return self._get_value(ReactorConfigKeys.CACHITO_KEY, fallback={})

    @property
    def allow_multiple_remote_sources(self):
        return self._get_value(ReactorConfigKeys.ALLOW_MULTIPLE_REMOTE_SOURCES_KEY, fallback=False)

    @property
    def artifacts_allowed_domains(self):
        return self._get_value(ReactorConfigKeys.ARTIFACTS_ALLOWED_DOMAINS_KEY, fallback=[])

    @property
    def yum_repo_allowed_domains(self):
        return self._get_value(ReactorConfigKeys.YUM_REPO_ALLOWED_DOMAINS_KEY, fallback=[])

    @property
    def image_labels(self):
        return self._get_value(ReactorConfigKeys.IMAGE_LABELS_KEY, fallback={})

    @property
    def image_label_info_url_format(self):
        return self._get_value(ReactorConfigKeys.IMAGE_LABEL_INFO_URL_FORMAT_KEY, fallback=None)

    @property
    def image_equal_labels(self):
        return self._get_value(ReactorConfigKeys.IMAGE_EQUAL_LABELS_KEY, fallback=[])

    @property
    def openshift(self):
        return self._get_value(ReactorConfigKeys.OPENSHIFT_KEY)

    @property
    def group_manifests(self):
        return self._get_value(ReactorConfigKeys.GROUP_MANIFESTS_KEY, fallback=True)

    @property
    def prefer_schema1_digest(self):
        return self._get_value(ReactorConfigKeys.PREFER_SCHEMA1_DIGEST_KEY, fallback=False)

    @property
    def content_versions(self):
        return self._get_value(ReactorConfigKeys.CONTENT_VERSIONS_KEY, fallback=[])

    @property
    def registries_organization(self):
        return self._get_value(ReactorConfigKeys.REGISTRIES_ORGANIZATION_KEY, fallback=None)

    @property
    def registry(self):
        all_registries = self._get_value(ReactorConfigKeys.REGISTRIES_KEY)

        registry = all_registries[0]

        reguri = RegistryURI(registry.get('url'))
        regdict = {'uri': reguri.docker_uri, 'version': reguri.version}
        if registry.get('auth'):
            regdict['secret'] = registry['auth']['cfg_path']
        regdict['insecure'] = registry.get('insecure', False)
        regdict['expected_media_types'] = registry.get('expected_media_types', [])

        return regdict

    @property
    def docker_registry(self):
        all_registries = self._get_value(ReactorConfigKeys.REGISTRIES_KEY)

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

    @property
    def yum_proxy(self):
        return self._get_value(ReactorConfigKeys.YUM_PROXY_KEY, fallback=None)

    def _as_source_registry(self, registry):
        return {
            'uri': RegistryURI(registry['url']),
            'insecure': registry.get('insecure', False),
            'dockercfg_path': registry.get('auth', {}).get('cfg_path', None)
        }

    @property
    def source_registry(self):
        source_registry = self._get_value(ReactorConfigKeys.SOURCE_REGISTRY_KEY)
        return self._as_source_registry(source_registry)

    @property
    def pull_registries(self):
        """
        Get list of pull_registries from config map, list entries follow the same
        format as the result of source_registry
        """
        pull_registries = self._get_value(ReactorConfigKeys.PULL_REGISTRIES_KEY, fallback=[])
        return [self._as_source_registry(reg) for reg in pull_registries]

    @property
    def sources_command(self):
        return self._get_value(ReactorConfigKeys.SOURCES_COMMAND_KEY, fallback=None)

    @property
    def required_secrets(self):
        return self._get_value(ReactorConfigKeys.REQUIRED_SECRETS_KEY, fallback=[])

    @property
    def worker_token_secrets(self):
        return self._get_value(ReactorConfigKeys.WORKER_TOKEN_SECRETS_KEY, fallback=[])

    @property
    def clusters(self):
        return self._get_value(ReactorConfigKeys.CLUSTERS_KEY, fallback={})

    @property
    def platform_descriptors(self):
        return self._get_value(ReactorConfigKeys.PLATFORM_DESCRIPTORS_KEY, fallback=[])

    @property
    def platform_to_goarch_mapping(self):
        return DefaultKeyDict(
            (descriptor['platform'], descriptor['architecture'])
            for descriptor in self.platform_descriptors)

    @property
    def goarch_to_platform_mapping(self):
        return DefaultKeyDict(
            (descriptor['architecture'], descriptor['platform'])
            for descriptor in self.platform_descriptors)

    @property
    def build_image_override(self):
        return self._get_value(ReactorConfigKeys.BUILD_IMAGE_OVERRIDE_KEY, fallback={})

    @property
    def flatpak(self):
        return self._get_value(ReactorConfigKeys.FLATPAK_KEY, fallback={})

    @property
    def flatpak_base_image(self):
        return self.flatpak['base_image']

    @property
    def flatpak_metadata(self):
        return self.flatpak['metadata']

    @property
    def package_comparison_exceptions(self):
        return set(self._get_value(ReactorConfigKeys.PACKAGE_COMPARISON_EXCEPTIONS_KEY,
                                   fallback=[]))

    @property
    def hide_files(self):
        return self._get_value(ReactorConfigKeys.HIDE_FILES_KEY, fallback={})

    @property
    def skip_koji_check_for_base_image(self):
        return self._get_value(ReactorConfigKeys.SKIP_KOJI_CHECK_FOR_BASE_IMAGE_KEY,
                               fallback=False)

    @property
    def deep_manifest_list_inspection(self):
        return self._get_value(ReactorConfigKeys.DEEP_MANIFEST_LIST_INSPECTION_KEY, fallback=True)

    @property
    def fail_on_digest_mismatch(self):
        return self._get_value(ReactorConfigKeys.FAIL_ON_DIGEST_MISMATCH_KEY, fallback=True)

    @property
    def source_container(self):
        return self._get_value(ReactorConfigKeys.SOURCE_CONTAINER_KEY, fallback={})

    @property
    def operator_manifests(self):
        return self._get_value(ReactorConfigKeys.OPERATOR_MANIFESTS_KEY, fallback={})

    @property
    def image_size_limit(self):
        config = self._get_value(ReactorConfigKeys.IMAGE_SIZE_LIMIT_KEY, fallback={})
        return {
            'binary_image': config.get('binary_image', 0),
        }

    @property
    def builder_ca_bundle(self):
        return self._get_value(ReactorConfigKeys.BUILDER_CA_BUNDLE_KEY, fallback=None)
