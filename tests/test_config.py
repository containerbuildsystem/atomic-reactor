"""
Copyright (c) 2021-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import io
import logging
import os
import pkg_resources
import pytest
from textwrap import dedent
import yaml
import smtplib
import requests_gssapi

import atomic_reactor
import koji
from atomic_reactor.util import read_yaml, DockerfileImages
import atomic_reactor.utils.cachito
import atomic_reactor.utils.koji
import atomic_reactor.utils.odcs
import osbs.conf
import osbs.api
from osbs.utils import RegistryURI, ImageName
from osbs.exceptions import OsbsValidationException
from tests.constants import REACTOR_CONFIG_MAP
from flexmock import flexmock
from atomic_reactor.config import (Configuration, ODCSConfig, get_koji_session, get_odcs_session,
                                   get_cachito_session, get_smtp_session, get_openshift_session)
from atomic_reactor.constants import REACTOR_CONFIG_ENV_NAME


REQUIRED_CONFIG = """\
version: 1
koji:
  hub_url: /
  root_url: ''
  auth: {}
openshift:
  url: openshift_url
source_registry:
  url: source_registry.com
registry:
  url: registry_url
"""


class TestConfiguration(object):
    @pytest.mark.parametrize(('config', 'exc'), [
        # Only API version v2 is valid.
        (
            dedent("""\
            registry:
              url: https://container-registry.example.com/v2
            registries_cfg_path: /var/run/secrets/atomic-reactor/v2-registry-dockercfg
            """),
            None,
        ),
        (
            dedent("""\
            registry:
              url: https://container-registry.example.com/v2
            """),
            None,
        ),
        (
            dedent("""\
            registry:
              url: https://container-registry.example.com/v2
            registries_cfg_path: /var/run/secrets/atomic-reactor/v2-registry-dockercfg
            """),
            None,
        ),
        # API version v1 is invalid.
        (
            dedent("""\
            registry:
              url: https://old-container-registry.example.com/v1
            registries_cfg_path: /var/run/secrets/atomic-reactor/v1-registry-dockercfg
            """),
            pytest.raises(OsbsValidationException, match="Invalid API version requested in .+")
        ),
        # Only API version v2 is valid.
        (
            dedent("""\
            registry:
              url: https://wrong-container-registry.example.com/v3
            registries_cfg_path: /var/run/secrets/atomic-reactor/wrong-registry-dockercfg
            """),
            pytest.raises(RuntimeError, match="Expected V2 registry but none in REACTOR_CONFIG")
        ),
    ])
    def test_get_registry(self, config, exc):
        required_config = dedent("""\
        version: 1
        koji:
          hub_url: /
          root_url: ''
          auth: {}
        openshift:
          url: openshift_url
        source_registry:
          url: source_registry.com
        """)
        config += "\n" + required_config
        config_json = read_yaml(config, 'schemas/config.json')

        expected = {
            'uri': 'container-registry.example.com',
            'insecure': False,
            'expected_media_types': [],
            'version': 'v2',
        }
        if 'registries_cfg_path' in config:
            expected['secret'] = '/var/run/secrets/atomic-reactor/v2-registry-dockercfg'
        conf = Configuration(raw_config=config_json)

        if exc is None:
            assert conf.registry == expected
        else:
            with exc:
                getattr(conf, 'registry')

    @pytest.mark.parametrize(('config', 'expected'), [
        ("pull_registries: []", []),
        (
            dedent("""\
            pull_registries:
            - url: registry.io
            """),
            [
                {
                    "uri": RegistryURI("registry.io"),
                    "insecure": False,
                    "dockercfg_path": None,
                }
            ],
        ),
        (
            dedent("""\
            pull_registries:
            - url: https://registry.io
            """),
            [
                {
                    "uri": RegistryURI("https://registry.io"),
                    "insecure": False,
                    "dockercfg_path": None,
                },
            ],
        ),
        (
            dedent("""\
            pull_registries:
            - url: https://registry.io
            registries_cfg_path: /var/run/secrets/atomic-reactor/v2-registry-dockercfg
            """),
            [
                {
                    "uri": RegistryURI("https://registry.io"),
                    "insecure": False,
                    "dockercfg_path": '/var/run/secrets/atomic-reactor/v2-registry-dockercfg',
                },
            ],
        ),
        (
            dedent("""\
            pull_registries:
            - url: registry.io
              insecure: true
            registries_cfg_path: /var/run/secrets/atomic-reactor/v2-registry-dockercfg
            """),
            [
                {
                    "uri": RegistryURI("registry.io"),
                    "insecure": True,
                    "dockercfg_path": "/var/run/secrets/atomic-reactor/v2-registry-dockercfg",
                },
            ],
        ),
        (
            dedent("""\
            pull_registries:
            - url: registry.io
              insecure: true
            - url: registry.org
            registries_cfg_path: /var/run/secrets/atomic-reactor/v2-registry-dockercfg
            """),
            [
                {
                    "uri": RegistryURI("registry.io"),
                    "insecure": True,
                    "dockercfg_path": "/var/run/secrets/atomic-reactor/v2-registry-dockercfg",
                },
                {
                    "uri": RegistryURI("registry.org"),
                    "insecure": False,
                    "dockercfg_path": "/var/run/secrets/atomic-reactor/v2-registry-dockercfg",
                },
            ],
        ),
    ])
    def test_get_pull_registries(self, config, expected):
        config += "\n" + REQUIRED_CONFIG

        config_json = read_yaml(config, 'schemas/config.json')
        conf = Configuration(raw_config=config_json)

        if isinstance(expected, list):
            pull_registries = conf.pull_registries

            # RegistryURI does not implement equality, check URI as string
            for reg in pull_registries + expected:
                reg['uri'] = reg['uri'].uri

            assert pull_registries == expected
        else:
            with expected:
                print(conf.pull_registries)

    @pytest.mark.parametrize(('config', 'expected_slots_dir', 'expected_enabled_hosts'), [
        ("""\
remote_hosts:
  slots_dir: path/foo
  pools:
    x86_64:
      remote-host1.x86_64:
        enabled: true
        auth: foo
        username: bar
        slots: 1
        socket_path: /user/foo/podman.sock
      remote-host2.x86_64:
        enabled: false
        auth: foo
        username: bar
        slots: 2
        socket_path: /user/foo/podman.sock
    ppc64le:
      remote-host3.ppc64le:
        enabled: true
        auth: foo
        username: bar
        slots: 3
        socket_path: /user/foo/podman.sock
         """,
         'path/foo', {'x86_64': ['remote-host1.x86_64'], 'ppc64le': ['remote-host3.ppc64le']}),
    ])
    def test_get_remote_hosts(self, config, expected_slots_dir, expected_enabled_hosts):
        config += "\n" + REQUIRED_CONFIG
        config_json = read_yaml(config, 'schemas/config.json')

        conf = Configuration(raw_config=config_json)

        remote_hosts = conf.remote_hosts
        assert expected_slots_dir == remote_hosts['slots_dir']

        pools = remote_hosts['pools']
        assert len(pools), 'Remote hosts do not have 2 architectures'
        assert len(pools['x86_64']) == 2, '2 entries expected for x86_64 architecture'
        assert sorted(pools['x86_64']) == sorted(['remote-host1.x86_64', 'remote-host2.x86_64'])

        assert len(pools['ppc64le']) == 1, '1 entry expected for ppc64le architecture'

        host1_x86_64 = pools['x86_64']['remote-host1.x86_64']
        assert host1_x86_64['auth'] == 'foo', 'Unexpected SSH key path'
        assert host1_x86_64['socket_path'] == '/user/foo/podman.sock', 'Unexpected socket path'

        host2_x86_64 = pools['x86_64']['remote-host2.x86_64']
        assert host2_x86_64['username'] == 'bar', 'Unexpected user name'
        host3_ppc64le = pools['ppc64le']['remote-host3.ppc64le']
        assert host3_ppc64le['slots'] == 3, 'Unexpected number of slots'

        for arch in ['x86_64', 'ppc64le']:
            enabled_hosts = [host for host, items in pools[arch].items() if items['enabled']]
            assert enabled_hosts == expected_enabled_hosts[arch]

    @pytest.mark.parametrize('config, error', [
        ("""\
remote_hosts: []
         """,
         "is not of type {!r}".format("object")),
        ("""\
remote_hosts:
  slots_dir: path/foo
         """,
         "{!r} is a required property".format("pools")),
        ("""\
remote_hosts:
  pools:
    x86_64:
      remote-host1.x86_64:
        enabled: true
        auth: foo
        username: bar
        slots: 1
        socket_path: /user/foo/podman.sock
         """,
         "{!r} is a required property".format("slots_dir")),
        ("""\
remote_hosts:
  pools:
    amd-64:
      remote-host1:
        enabled: true
        auth: foo
        username: bar
        slots: 1
        socket_path: /user/foo/podman.sock
         """,
         "{!r} does not match any of the regexes".format("amd-64")),
        ("""\
remote_hosts:
  slots_dir: path/foo
  pools:
    s390x:
      remote-host1:
        auth: foo
        username: bar
        slots: 1
        socket_path: /user/foo/podman.sock
         """,
         "{!r} is a required property".format("enabled")),
        ("""\
remote_hosts:
  slots_dir: path/foo
  pools:
    s390x:
      remote-host1.s390x:
        enabled: true
        username: bar
        slots: 1
        socket_path: /user/foo/podman.sock
         """,
         "{!r} is a required property".format("auth")),
        ("""\
remote_hosts:
  slots_dir: path/foo
  pools:
    s390x:
      remote-host1.s390x:
        enabled: true
        auth: foo
        slots: 1
        socket_path: /user/foo/podman.sock
         """,
         "{!r} is a required property".format("username")),
        ("""\
remote_hosts:
  slots_dir: path/foo
  pools:
    s390x:
      remote-host1.s390x:
        enabled: true
        auth: foo
        username: bar
        slots: 1
         """,
         "{!r} is a required property".format("socket_path")),
        ("""\
remote_hosts:
  slots_dir: path/foo
  pools:
    s390x:
      remote-host1.s390x:
        enabled: true
        auth: foo
        username: bar
        socket_path: /user/foo/podman.sock
         """,
         "{!r} is a required property".format("slots")),
        ("""\
remote_hosts:
  slots_dir: path/foo
  pools:
    aarch64:
      remote-host1.@aarch64@@:
        enabled: true
        auth: foo
        username: bar
        socket_path: /user/foo/podman.sock
         """,
         "{!r} does not match any of the regexes".format("remote-host1.@aarch64@@")),
    ])
    def test_get_remote_hosts_schema_validation(self, config, error):
        config += "\n" + REQUIRED_CONFIG
        with pytest.raises(OsbsValidationException) as exc_info:
            read_yaml(config, 'schemas/config.json')
        assert error in str(exc_info.value)

    @pytest.mark.parametrize('config, error', [
        ("""\
pull_registries: {}
         """,
         "is not of type {!r}".format("array")),
        ("""\
pull_registries:
- insecure: false
         """,
         "{!r} is a required property".format("url")),
    ])
    def test_get_pull_registries_schema_validation(self, config, error):
        config += "\n" + REQUIRED_CONFIG
        with pytest.raises(OsbsValidationException) as exc_info:
            read_yaml(config, 'schemas/config.json')
        assert error in str(exc_info.value)

    def test_filename(self, tmpdir):
        filename = os.path.join(str(tmpdir), 'config.yaml')
        with open(filename, 'w') as fp:
            fp.write(dedent(REQUIRED_CONFIG))

        Configuration(config_path=filename)

    def test_no_schema_resource(self, tmpdir, caplog):
        class FakeProvider(object):
            def get_resource_stream(self, pkg, rsc):
                raise IOError

        # pkg_resources.resource_stream() cannot be mocked directly
        # Instead mock the module-level function it calls.
        (flexmock(pkg_resources)
            .should_receive('get_provider')
            .and_return(FakeProvider()))

        filename = os.path.join(str(tmpdir), 'config.yaml')
        with open(filename, 'w'):
            pass

        with caplog.at_level(logging.ERROR), pytest.raises(Exception):
            Configuration(config_path=filename)

        captured_errs = [x.message for x in caplog.records]
        assert "unable to extract JSON schema, cannot validate" in captured_errs

    @pytest.mark.parametrize('schema', [
        # Invalid JSON
        '{',

        # Invalid schema
        '{"properties": {"any": null}}',
    ])
    def test_invalid_schema_resource(self, tmpdir, caplog, schema):
        class FakeProvider(object):
            def get_resource_stream(self, pkg, rsc):
                return io.BufferedReader(io.BytesIO(schema))

        # pkg_resources.resource_stream() cannot be mocked directly
        # Instead mock the module-level function it calls.
        (flexmock(pkg_resources)
            .should_receive('get_provider')
            .and_return(FakeProvider()))

        filename = os.path.join(str(tmpdir), 'config.yaml')
        with open(filename, 'w'):
            pass

        with caplog.at_level(logging.ERROR), pytest.raises(Exception):
            Configuration(config_path=filename)

        captured_errs = [x.message for x in caplog.records]
        assert any("cannot validate" in x for x in captured_errs)

    def test_bad_version(self, tmpdir):
        filename = os.path.join(str(tmpdir), 'config.yaml')
        with open(filename, 'w') as fp:
            fp.write(dedent("""\
                version: 2
                koji:
                  hub_url: /
                  root_url: ''
                  auth: {}
                openshift:
                  url: openshift_url
                source_registry:
                  url: source_registry.com
                registry:
                  url: registry_url
            """))

        with pytest.raises(ValueError):
            Configuration(config_path=filename)

    @pytest.mark.parametrize('default', (
        'release',
        'beta',
        'unsigned',
    ))
    def test_odcs_config(self, tmpdir, default):
        config = """\
odcs:
  signing_intents:
  - name: release
    keys: [R123, R234]
  - name: beta
    keys: [R123, B456, B457]
  - name: unsigned
    keys: []
  default_signing_intent: {default}
  api_url: http://odcs.example.com
  auth:
    ssl_certs_dir: /var/run/secrets/atomic-reactor/odcssecret
""".format(default=default)

        config += "\n" + REQUIRED_CONFIG
        filename = str(tmpdir.join('config.yaml'))
        with open(filename, 'w') as fp:
            fp.write(dedent(config))

        conf = Configuration(config_path=filename)

        odcs_config = conf.odcs_config

        assert odcs_config.default_signing_intent == default

        unsigned_intent = {'name': 'unsigned', 'keys': [], 'restrictiveness': 0}
        beta_intent = {'name': 'beta', 'keys': ['R123', 'B456', 'B457'], 'restrictiveness': 1}
        release_intent = {'name': 'release', 'keys': ['R123', 'R234'], 'restrictiveness': 2}
        assert odcs_config.signing_intents == [
            unsigned_intent, beta_intent, release_intent
        ]
        assert odcs_config.get_signing_intent_by_name('release') == release_intent
        assert odcs_config.get_signing_intent_by_name('beta') == beta_intent
        assert odcs_config.get_signing_intent_by_name('unsigned') == unsigned_intent

        with pytest.raises(ValueError):
            odcs_config.get_signing_intent_by_name('missing')

        assert odcs_config.get_signing_intent_by_keys(['R123', 'R234'])['name'] == 'release'
        assert odcs_config.get_signing_intent_by_keys('R123 R234')['name'] == 'release'
        assert odcs_config.get_signing_intent_by_keys(['R123'])['name'] == 'release'
        assert odcs_config.get_signing_intent_by_keys('R123')['name'] == 'release'
        assert odcs_config.get_signing_intent_by_keys(['R123', 'B456'])['name'] == 'beta'
        assert odcs_config.get_signing_intent_by_keys(['B456', 'R123'])['name'] == 'beta'
        assert odcs_config.get_signing_intent_by_keys('B456 R123')['name'] == 'beta'
        assert odcs_config.get_signing_intent_by_keys('R123 B456 ')['name'] == 'beta'
        assert odcs_config.get_signing_intent_by_keys(['B456'])['name'] == 'beta'
        assert odcs_config.get_signing_intent_by_keys('B456')['name'] == 'beta'
        assert odcs_config.get_signing_intent_by_keys([])['name'] == 'unsigned'
        assert odcs_config.get_signing_intent_by_keys('')['name'] == 'unsigned'

        with pytest.raises(ValueError):
            assert odcs_config.get_signing_intent_by_keys(['missing'])
        with pytest.raises(ValueError):
            assert odcs_config.get_signing_intent_by_keys(['R123', 'R234', 'B457'])

    def test_odcs_config_invalid_default_signing_intent(self, tmpdir):
        config = """\
odcs:
  signing_intents:
  - name: release
    keys: [R123]
  - name: beta
    keys: [R123, B456]
  - name: unsigned
    keys: []
  default_signing_intent: spam
  api_url: http://odcs.example.com
  auth:
    ssl_certs_dir: /var/run/secrets/atomic-reactor/odcssecret
"""
        config += "\n" + REQUIRED_CONFIG
        filename = str(tmpdir.join('config.yaml'))
        with open(filename, 'w') as fp:
            fp.write(dedent(config))

        conf = Configuration(config_path=filename)

        with pytest.raises(ValueError) as exc_info:
            getattr(conf, 'odcs_config')
        message = str(exc_info.value)
        assert message == dedent("""\
            unknown signing intent name "spam", valid names: unsigned, beta, release
            """.rstrip())

    def test_odcs_config_deprecated_signing_intent(self, tmpdir, caplog):
        config = """\
odcs:
  signing_intents:
  - name: release
    keys: [R123]
    deprecated_keys: [R122]
  default_signing_intent: release
  api_url: http://odcs.example.com
  auth:
    ssl_certs_dir: /var/run/secrets/atomic-reactor/odcssecret
"""
        config += "\n" + REQUIRED_CONFIG
        filename = str(tmpdir.join('config.yaml'))
        with open(filename, 'w') as fp:
            fp.write(dedent(config))

        conf = Configuration(config_path=filename)

        odcs_config = conf.odcs_config
        signing_intent = odcs_config.get_signing_intent_by_keys(['R123'])
        assert signing_intent['name'] == 'release'
        assert 'contain deprecated entries' not in caplog.text

        signing_intent = odcs_config.get_signing_intent_by_keys(['R123', 'R122'])
        assert signing_intent['name'] == 'release'
        assert 'contain deprecated entries' in caplog.text

    @pytest.mark.parametrize('parse_from', ['env', 'file', 'raw'])
    @pytest.mark.parametrize('method', [
        'odcs', 'smtp', 'artifacts_allowed_domains', 'yum_repo_allowed_domains', 'image_labels',
        'image_label_info_url_format', 'image_equal_labels', 'fail_on_digest_mismatch',
        'openshift', 'group_manifests', 'platform_descriptors', 'registry', 'yum_proxy',
        'source_registry', 'sources_command', 'hide_files', 'skip_koji_check_for_base_image',
        'deep_manifest_list_inspection'
    ])
    def test_get_methods(self, parse_from, method, tmpdir, caplog, monkeypatch):
        if parse_from == 'raw':
            conf = Configuration(raw_config=yaml.safe_load(REACTOR_CONFIG_MAP))
        elif parse_from == 'env':
            monkeypatch.setenv(REACTOR_CONFIG_ENV_NAME, dedent(REACTOR_CONFIG_MAP))
            conf = Configuration(env_name=REACTOR_CONFIG_ENV_NAME)
        elif parse_from == 'file':
            filename = str(tmpdir.join('config.yaml'))
            with open(filename, 'w') as fp:
                fp.write(dedent(REACTOR_CONFIG_MAP))
            conf = Configuration(config_path=filename)

        real_attr = getattr(conf, method)

        output = real_attr
        reactor_config_map = yaml.safe_load(REACTOR_CONFIG_MAP)

        if method == 'registry':
            expected = reactor_config_map['registry']
        else:
            expected = reactor_config_map[method]

        if method == 'registry':
            # since there will only be exactly one registry
            registry = expected
            reguri = RegistryURI(registry.get('url'))
            regdict = {'uri': reguri.docker_uri, 'version': reguri.version}
            regdict['secret'] = reactor_config_map['registries_cfg_path']
            regdict['insecure'] = registry.get('insecure', False)
            regdict['expected_media_types'] = registry.get('expected_media_types', [])

            assert output == regdict
            return

        if method == 'source_registry':
            expect = {
                'uri': RegistryURI(expected['url']),
                'insecure': expected.get('insecure', False)
            }
            assert output['insecure'] == expect['insecure']
            assert output['uri'].uri == expect['uri'].uri
            return

        assert output == expected
        os.environ.pop(REACTOR_CONFIG_ENV_NAME, None)

        if parse_from == 'raw':
            log_msg = "reading config from raw_config kwarg"
        elif parse_from == 'env':
            log_msg = f"reading config from {REACTOR_CONFIG_ENV_NAME} env variable"
        elif parse_from == 'file':
            log_msg = f"reading config from {filename}"
        assert log_msg in caplog.text

    @pytest.mark.parametrize(('config', 'expect'), [
        ("""\
platform_descriptors:
  - platform: x86_64
    architecture: amd64
         """,
         {'x86_64': 'amd64',
          'ppc64le': 'ppc64le'}),
    ])
    def test_get_platform_to_goarch_mapping(self, config, expect):
        config += "\n" + REQUIRED_CONFIG

        config_json = read_yaml(config, 'schemas/config.json')

        conf = Configuration(raw_config=config_json)

        platform_to_goarch = conf.platform_to_goarch_mapping
        goarch_to_platform = conf.goarch_to_platform_mapping
        for plat, goarch in expect.items():
            assert platform_to_goarch[plat] == goarch
            assert goarch_to_platform[goarch] == plat

    @pytest.mark.parametrize(('config', 'expect'), [
        ("""\
flatpak:
  base_image: fedora:latest
         """,
         "fedora:latest"),
        ("""\
         """,
         None),
        ("""\
flatpak: {}
         """,
         None),
    ])
    def test_get_flatpak_base_image(self, config, expect):
        config += "\n" + REQUIRED_CONFIG
        config_json = read_yaml(config, 'schemas/config.json')

        conf = Configuration(raw_config=config_json)

        if expect:
            base_image = conf.flatpak_base_image
            assert base_image == expect
        else:
            with pytest.raises(KeyError):
                getattr(conf, 'flatpak_base_image')

    @pytest.mark.parametrize(('config', 'expect'), [
        ("""\
flatpak:
  metadata: labels
         """,
         "labels"),
        ("""\
         """,
         None),
        ("""\
flatpak: {}
         """,
         None),
    ])
    def test_get_flatpak_metadata(self, config, expect):
        config += "\n" + REQUIRED_CONFIG
        config_json = read_yaml(config, 'schemas/config.json')

        conf = Configuration(raw_config=config_json)

        if expect:
            base_image = conf.flatpak_metadata
            assert base_image == expect
        else:
            with pytest.raises(KeyError):
                getattr(conf, 'flatpak_metadata')

    @pytest.mark.parametrize(('config', 'raise_error'), [
        ("""\
koji:
  hub_url: https://koji.example.com/hub
  root_url: https://koji.example.com/root
  auth:
    proxyuser: proxyuser
    krb_principal: krb_principal
    krb_keytab_path: /tmp/krb_keytab
        """, False),

        ("""\
koji:
  hub_url: https://koji.example.com/hub
  root_url: https://koji.example.com/root
  auth:
    proxyuser: proxyuser
    krb_principal: krb_principal
    krb_keytab_path: /tmp/krb_keytab
  use_fast_upload: false
        """, False),

        ("""\
koji:
  hub_url: https://koji.example.com/hub
  root_url: https://koji.example.com/root
  auth:
    proxyuser: proxyuser
    ssl_certs_dir: /var/certs
        """, False),

        ("""\
koji:
  hub_url: https://koji.example.com/hub
  root_url: https://koji.example.com/root
  auth:
    proxyuser: proxyuser
        """, False),

        ("""\
koji:
  hub_url: https://koji.example.com/hub
  root_url: https://koji.example.com/root
  auth:
        """, True),

        ("""\
koji:
  hub_url: https://koji.example.com/hub
  root_url: https://koji.example.com/root
  auth:
    proxyuser: proxyuser
    krb_principal: krb_principal
    krb_keytab_path: /tmp/krb_keytab
    ssl_certs_dir: /var/certs
        """, True),

        ("""\
koji:
  hub_url: https://koji.example.com/hub
  root_url: https://koji.example.com/root
  auth:
    proxyuser: proxyuser
    krb_keytab_path: /tmp/krb_keytab
        """, True),

        ("""\
koji:
  hub_url: https://koji.example.com/hub
  root_url: https://koji.example.com/root
  auth:
    proxyuser: proxyuser
    krb_principal: krb_principal
        """, True),

        ("""\
koji:
  hub_url: https://koji.example.com/hub
  root_url: https://koji.example.com/root
  auth:
    proxyuser: proxyuser
    krb_principal: krb_principal
    ssl_certs_dir: /var/certs
        """, True),

        ("""\
koji:
  hub_url: https://koji.example.com/hub
  root_url: https://koji.example.com/root
  auth:
    proxyuser: proxyuser
    krb_keytab_path: /tmp/krb_keytab
    ssl_certs_dir: /var/certs
        """, True),
    ])
    def test_get_koji_session(self, config, raise_error):
        required_config = """\
version: 1
source_registry:
  url: source_registry.com
registry:
  url: registry_url
openshift:
  url: openshift_url
"""
        config += "\n" + required_config
        if raise_error:
            with pytest.raises(Exception):
                read_yaml(config, 'schemas/config.json')
            return
        config_json = read_yaml(config, 'schemas/config.json')

        auth_info = {
            "proxyuser": config_json['koji']['auth'].get('proxyuser'),
            "ssl_certs_dir": config_json['koji']['auth'].get('ssl_certs_dir'),
            "krb_principal": config_json['koji']['auth'].get('krb_principal'),
            "krb_keytab": config_json['koji']['auth'].get('krb_keytab_path')
        }

        use_fast_upload = config_json['koji'].get('use_fast_upload', True)

        conf = Configuration(raw_config=config_json)

        (flexmock(atomic_reactor.utils.koji)
            .should_receive('create_koji_session')
            .with_args(config_json['koji']['hub_url'], auth_info, use_fast_upload)
            .once()
            .and_return(True))

        get_koji_session(conf)

    @pytest.mark.parametrize('root_url', (
        'https://koji.example.com/root',
        'https://koji.example.com/root/',
        None
    ))
    def test_get_koji_path_info(self, root_url):

        config = {
            'version': 1,
            'koji': {
                'hub_url': 'https://koji.example.com/hub',
                'auth': {
                    'ssl_certs_dir': '/var/certs'
                }
            },
            'openshift': {'url': 'openshift_url'},
            'source_registry': {'url': 'source_registry'},
            'registry': {'url': 'registry_url'}
        }
        expected_root_url = 'https://koji.example.com/root'

        if root_url:
            config['koji']['root_url'] = root_url

        config_yaml = yaml.safe_dump(config)

        expect_error = not root_url
        if expect_error:
            with pytest.raises(Exception):
                read_yaml(config_yaml, 'schemas/config.json')
            return

        parsed_config = read_yaml(config_yaml, 'schemas/config.json')

        conf = Configuration(raw_config=parsed_config)

        (flexmock(koji.PathInfo)
            .should_receive('__init__')
            .with_args(topdir=expected_root_url)
            .once())
        getattr(conf, 'koji_path_info')

    @pytest.mark.parametrize(('config', 'raise_error'), [
        ("""\
odcs:
  api_url: https://odcs.example.com/api/1
  auth:
    ssl_certs_dir: /var/run/secrets/atomic-reactor/odcssecret
  signing_intents:
  - name: release
    keys: [R123]
  default_signing_intent: default
  timeout: 3600
        """, False),

        ("""\
odcs:
  api_url: https://odcs.example.com/api/1
  auth:
    ssl_certs_dir: nonexistent
  signing_intents:
  - name: release
    keys: [R123]
  default_signing_intent: default
        """, False),

        ("""\
odcs:
  api_url: https://odcs.example.com/api/1
  auth:
    openidc_dir: /var/run/open_idc
  signing_intents:
  - name: release
    keys: [R123]
  default_signing_intent: default
        """, False),

        ("""\
odcs:
  api_url: https://odcs.example.com/api/1
  auth:
    openidc_dir: /var/run/open_idc
    ssl_certs_dir: /var/run/secrets/atomic-reactor/odcssecret
  signing_intents:
  - name: release
    keys: [R123]
  default_signing_intent: default
        """, True),

        ("""\
odcs:
  api_url: https://odcs.example.com/api/1
  auth:
    openidc_dir: /var/run/open_idc
  signing_intents:
  - name: release
    keys: [R123]
        """, True),

        ("""\
odcs:
  api_url: https://odcs.example.com/api/1
  auth:
    openidc_dir: /var/run/open_idc
  default_signing_intent: default
        """, True),

        ("""\
odcs:
  auth:
    openidc_dir: /var/run/open_idc
  signing_intents:
  - name: release
    keys: [R123]
  default_signing_intent: default
        """, True),
    ])
    def test_get_odcs_session(self, tmpdir, config, raise_error):
        config += "\n" + REQUIRED_CONFIG

        if raise_error:
            with pytest.raises(Exception):
                read_yaml(config, 'schemas/config.json')
            return
        config_json = read_yaml(config, 'schemas/config.json')

        auth_info = {
            'insecure': config_json['odcs'].get('insecure', False),
            'timeout': config_json['odcs'].get('timeout', None),
        }
        if 'openidc_dir' in config_json['odcs']['auth']:
            config_json['odcs']['auth']['openidc_dir'] = str(tmpdir)
            filename = str(tmpdir.join('token'))
            with open(filename, 'w') as fp:
                fp.write("my_token")
            auth_info['token'] = "my_token"

        ssl_dir_raise = False
        if 'ssl_certs_dir' in config_json['odcs']['auth']:
            if config_json['odcs']['auth']['ssl_certs_dir'] != "nonexistent":
                config_json['odcs']['auth']['ssl_certs_dir'] = str(tmpdir)
                filename = str(tmpdir.join('cert'))
                with open(filename, 'w') as fp:
                    fp.write("my_cert")
                auth_info['cert'] = filename
            else:
                ssl_dir_raise = True

        conf = Configuration(raw_config=config_json)

        if not ssl_dir_raise:
            (flexmock(atomic_reactor.utils.odcs.ODCSClient)
                .should_receive('__init__')
                .with_args(config_json['odcs']['api_url'], **auth_info)
                .once()
                .and_return(None))

            get_odcs_session(conf)
        else:
            with pytest.raises(KeyError):
                get_odcs_session(conf)

    def test_get_odcs_session_krb_keytab_path(self, tmp_path):
        keytab = tmp_path / "keytab"
        keytab.write_text("fake keytab")

        config = dedent(f"""
        odcs:
          api_url: https://odcs.example.com/api/1
          auth:
            krb_keytab_path: {keytab}
          signing_intents:
          - name: release
            keys: [R123]
          default_signing_intent: default
          timeout: 3600
         \n""") + REQUIRED_CONFIG

        config_json = read_yaml(config, 'schemas/config.json')
        conf = Configuration(raw_config=config_json)

        mock = flexmock()
        flexmock(requests_gssapi).should_receive("HTTPSPNEGOAuth").and_return(mock)

        expected_client_kwargs = {
            'insecure': config_json['odcs'].get('insecure', False),
            'timeout': config_json['odcs'].get('timeout', None),
            'kerberos_auth': mock
        }

        (flexmock(atomic_reactor.utils.odcs.ODCSClient)
         .should_receive('__init__')
         .with_args(config_json['odcs']['api_url'], **expected_client_kwargs)
         .once()
         .and_return(None))

        get_odcs_session(conf)
        assert os.getenv('KRB5_CLIENT_KTNAME') == str(keytab)

    def test_get_odcs_session_krb_keytab_path_nonexistent_keytab(self, tmp_path):
        config = dedent("""
        odcs:
          api_url: https://odcs.example.com/api/1
          auth:
            krb_keytab_path: /tmp/nonexistent_keytab
          signing_intents:
          - name: release
            keys: [R123]
          default_signing_intent: default
          timeout: 3600
         \n""") + REQUIRED_CONFIG

        config_json = read_yaml(config, 'schemas/config.json')
        conf = Configuration(raw_config=config_json)

        with pytest.raises(KeyError, match="ODCS krb_keytab_path doesn't exist"):
            get_odcs_session(conf)

    @pytest.mark.parametrize(('config', 'raise_error'), [
        ("""\
smtp:
  host: smtp.example.com
  from_address: osbs@example.com
        """, False),

        ("""\
smtp:
  from_address: osbs@example.com
        """, True),

        ("""\
smtp:
  host: smtp.example.com
        """, True),

        ("""\
smtp:
        """, True),
    ])
    def test_get_smtp_session(self, config, raise_error):
        config += "\n" + REQUIRED_CONFIG

        if raise_error:
            with pytest.raises(Exception):
                read_yaml(config, 'schemas/config.json')
            return
        config_json = read_yaml(config, 'schemas/config.json')

        conf = Configuration(raw_config=config_json)

        (flexmock(smtplib.SMTP)
            .should_receive('__init__')
            .with_args(config_json['smtp']['host'])
            .once()
            .and_return(None))

        get_smtp_session(conf)

    @pytest.mark.parametrize(('config', 'error'), [
        ("""\
cachito:
  api_url: https://cachito.example.com
  auth:
    ssl_certs_dir: /var/run/secrets/atomic-reactor/cachitosecret
  timeout: 1000
        """, False),

        ("""\
cachito:
  api_url: https://cachito.example.com
  insecure: true
  auth:
    ssl_certs_dir: /var/run/secrets/atomic-reactor/cachitosecret
        """, False),

        ("""\
cachito:
  api_url: https://cachito.example.com
  auth:
        """, OsbsValidationException),

        ("""\
cachito:
  api_url: https://cachito.example.com
        """, OsbsValidationException),

        ("""\
cachito:
  auth:
    ssl_certs_dir: /var/run/secrets/atomic-reactor/cachitosecret
        """, OsbsValidationException),

        ("""\
cachito:
  api_url: https://cachito.example.com
  auth:
    ssl_certs_dir: /var/run/secrets/atomic-reactor/cachitosecret
  spam: ham
        """, OsbsValidationException),

        ("""\
cachito:
  api_url: https://cachito.example.com
  auth:
    ssl_certs_dir: nonexistent
        """, False),
    ])
    def test_get_cachito_session(self, tmpdir, config, error):
        config += "\n" + REQUIRED_CONFIG

        if error:
            with pytest.raises(error):
                read_yaml(config, 'schemas/config.json')
            return
        config_json = read_yaml(config, 'schemas/config.json')

        auth_info = {
            'insecure': config_json['cachito'].get('insecure', False),
            'timeout': config_json['cachito'].get('timeout'),
        }

        ssl_dir_raise = False
        if 'ssl_certs_dir' in config_json['cachito']['auth']:
            if config_json['cachito']['auth']['ssl_certs_dir'] != "nonexistent":
                config_json['cachito']['auth']['ssl_certs_dir'] = str(tmpdir)
                filename = str(tmpdir.join('cert'))
                with open(filename, 'w') as fp:
                    fp.write("my_cert")
                auth_info['cert'] = filename
            else:
                ssl_dir_raise = True

        conf = Configuration(raw_config=config_json)

        if not ssl_dir_raise:
            (flexmock(atomic_reactor.utils.cachito.CachitoAPI)
                .should_receive('__init__')
                .with_args(config_json['cachito']['api_url'], **auth_info)
                .once()
                .and_return(None))

            get_cachito_session(conf)
        else:
            with pytest.raises(RuntimeError, match="Cachito ssl_certs_dir doesn't exist"):
                get_cachito_session(conf)

    @pytest.mark.parametrize(('config', 'raise_error'), [
        ("""\
openshift:
  url: https://openshift.example.com
  auth:
    ssl_certs_dir: /var/run/secrets/atomic-reactor/odcssecret
        """, False),

        ("""\
openshift:
  url: https://openshift.example.com
        """, False),

        ("""\
openshift:
  url: https://openshift.example.com
  auth:
    krb_principal: principal
    krb_keytab_path: /var/keytab
        """, False),

        ("""\
openshift:
  url: https://openshift.example.com
  auth:
    krb_principal: principal
    krb_keytab_path: /var/keytab
    krb_cache_path: /var/krb/cache
        """, False),

        ("""\
openshift:
  url: https://openshift.example.com
  auth:
    enable: True
        """, False),

        ("""\
openshift:
  url: https://openshift.example.com
  auth:
    krb_keytab_path: /var/keytab
        """, True),

        ("""\
openshift:
  url: https://openshift.example.com
  auth:
    krb_principal: principal
        """, True),

        ("""\
openshift:
  auth:
    ssl_certs_dir: /var/run/secrets/atomic-reactor/odcssecret
        """, True),

        ("""\
openshift:
  auth:
    krb_principal: principal
    krb_keytab_path: /var/keytab
        """, True),

        ("""\
openshift:
  url: https://openshift.example.com
  auth:
        """, True),

        ("""\
openshift:
  auth:
    ssl_certs_dir: /var/run/secrets/atomic-reactor/odcssecret
        """, True),
    ])
    def test_get_openshift_session(self, config, raise_error):
        required_config = """\
version: 1
koji:
  hub_url: /
  root_url: ''
  auth: {}
source_registry:
  url: source_registry.com
registry:
  url: registry_url
"""

        config += "\n" + required_config

        if raise_error:
            with pytest.raises(Exception):
                read_yaml(config, 'schemas/config.json')
            return
        config_json = read_yaml(config, 'schemas/config.json')

        auth_info = {
            'openshift_url': config_json['openshift']['url'],
            'verify_ssl': not config_json['openshift'].get('insecure', False),
            'use_auth': False,
            'conf_file': None,
            'namespace': 'namespace',
        }
        if config_json['openshift'].get('auth'):
            if config_json['openshift']['auth'].get('krb_keytab_path'):
                auth_info['kerberos_keytab'] =\
                    config_json['openshift']['auth'].get('krb_keytab_path')
            if config_json['openshift']['auth'].get('krb_principal'):
                auth_info['kerberos_principal'] =\
                    config_json['openshift']['auth'].get('krb_principal')
            if config_json['openshift']['auth'].get('krb_cache_path'):
                auth_info['kerberos_ccache'] =\
                    config_json['openshift']['auth'].get('krb_cache_path')
            if config_json['openshift']['auth'].get('ssl_certs_dir'):
                auth_info['client_cert'] =\
                    os.path.join(config_json['openshift']['auth'].get('ssl_certs_dir'), 'cert')
                auth_info['client_key'] =\
                    os.path.join(config_json['openshift']['auth'].get('ssl_certs_dir'), 'key')
            auth_info['use_auth'] = config_json['openshift']['auth'].get('enable', False)

        (flexmock(osbs.conf.Configuration)
            .should_call('__init__')
            .with_args(**auth_info)
            .once())
        (flexmock(osbs.api.OSBS)
            .should_call('__init__')
            .once())

        conf = Configuration(raw_config=config_json)
        get_openshift_session(conf, 'namespace')

    @pytest.mark.parametrize('config, valid', [
        ("""\
operator_manifests:
  allowed_registries: null
        """, True),  # minimal valid example, allows all registries
        ("""\
operator_manifests:
  allowed_registries:
    - foo
    - bar
  repo_replacements:
    - registry: foo
      package_mappings_url: https://somewhere.net/mapping.yaml
  registry_post_replace:
    - old: foo
      new: bar
        """, True),  # all known properties
        ("""\
operator_manifests: null
        """, False),  # has to be a dict
        ("""\
operator_manifests: {}
        """, False),  # allowed_registries is required
        ("""\
operator_manifests:
  allowed_registries: []
        """, False),  # if not null, allowed_registries must not be empty
        ("""\
operator_manifests:
  allowed_registries: null
  something_else: null
        """, False),  # additional properties not allowed
        ("""\
operator_manifests:
  allowed_registries: null
  registry_post_replace:
    - old: foo
        """, False),  # missing replacement registry
        ("""\
operator_manifests:
  allowed_registries: null
  registry_post_replace:
    - new: foo
        """, False),  # missing original registry
        ("""\
operator_manifests:
  allowed_registries: null
  repo_replacements:
    - registry: foo
        """, False),  # missing package mappings url
        ("""\
operator_manifests:
  allowed_registries: null
  repo_replacements:
    - package_mappings_url: https://somewhere.net/mapping.yaml
        """, False),  # missing registry
        ("""\
operator_manifests:
  allowed_registries: null,
  repo_replacements:
    - registry: foo
      package_mappings_url: mapping.yaml
        """, False),  # package mappings url is not a url
    ])
    def test_get_operator_manifests(self, tmpdir, config, valid):
        config += "\n" + REQUIRED_CONFIG
        if valid:
            read_yaml(config, 'schemas/config.json')
        else:
            with pytest.raises(OsbsValidationException):
                read_yaml(config, 'schemas/config.json')
            return

        filename = os.path.join(str(tmpdir), 'config.yaml')
        with open(filename, 'w') as fp:
            fp.write(dedent(config))
        conf = Configuration(config_path=filename)

        operator_config = conf.operator_manifests
        assert isinstance(operator_config, dict)
        assert "allowed_registries" in operator_config

    @pytest.mark.parametrize(('images_exist', 'organization'), [
        (True, None),
        (True, 'organization'),
        (False, None),
        (False, 'organization'),
    ])
    def test_update_dockerfile_images_from_config(self, tmp_path, images_exist, organization):
        config = REQUIRED_CONFIG

        if organization:
            config += "\nregistries_organization: " + organization

        config_yaml = tmp_path / 'config.yaml'
        config_yaml.write_text(dedent(config), "utf-8")

        if images_exist:
            parent_images = ['parent:latest', 'base:latest']
            if organization:
                expect_images = [ImageName.parse('source_registry.com/organization/base:latest'),
                                 ImageName.parse('source_registry.com/organization/parent:latest')]
            else:
                expect_images = [ImageName.parse('source_registry.com/base:latest'),
                                 ImageName.parse('source_registry.com/parent:latest')]
        else:
            parent_images = []

        dockerfile_images = DockerfileImages(parent_images)

        conf = Configuration(config_path=str(config_yaml))
        conf.update_dockerfile_images_from_config(dockerfile_images)

        if images_exist:
            assert len(dockerfile_images) == 2
            assert dockerfile_images.keys() == expect_images
        else:
            assert not dockerfile_images

    @pytest.mark.parametrize('param', [
        ("", 1),  # default
        ("remote_sources_default_version: 2", 2),
    ])
    def test_remote_sources_default_version(self, param):
        config, expected = param
        config += "\n" + REQUIRED_CONFIG

        config_json = read_yaml(config, 'schemas/config.json')
        conf = Configuration(raw_config=config_json)

        assert conf.remote_sources_default_version == expected


def test_ensure_odcsconfig_does_not_modify_original_signing_intents():
    signing_intents = [{'name': 'release', 'keys': ['R123', 'R234']}]
    odcs_config = ODCSConfig(signing_intents, 'release')
    assert [{
        'name': 'release',
        'keys': ['R123', 'R234'],
        'restrictiveness': 0
    }] == odcs_config.signing_intents
    # Verify original intent is not modified.
    assert 'restrictiveness' not in signing_intents[0]
