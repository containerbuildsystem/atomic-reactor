"""
Copyright (c) 2021 Red Hat, Inc
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
import re
import yaml
import smtplib

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
registries:
  - url: registry_url
"""


class TestConfiguration(object):
    @pytest.mark.parametrize(('config', 'exc'), [
        ("""\
registries:
- url: https://container-registry.example.com/v2
  auth:
    cfg_path: /var/run/secrets/atomic-reactor/v2-registry-dockercfg
         """,
         None),
        ("""\
registries:
- url: https://container-registry.example.com/v2
  auth:
    cfg_path: /var/run/secrets/atomic-reactor/v2-registry-dockercfg
- url: https://another-container-registry.example.com/
  auth:
    cfg_path: /var/run/secrets/atomic-reactor/another-registry-dockercfg
         """,
         None),
        ("""\
registries:
- url: https://old-container-registry.example.com/v1
  auth:
    cfg_path: /var/run/secrets/atomic-reactor/v1-registry-dockercfg
         """,
         OsbsValidationException),
        ("""\
registries:
- url: https://wrong-container-registry.example.com/v3
  auth:
    cfg_path: /var/run/secrets/atomic-reactor/wrong-registry-dockercfg
         """,
         RuntimeError),
    ])
    def test_get_docker_registry(self, config, exc):
        required_config = """\
version: 1
koji:
  hub_url: /
  root_url: ''
  auth: {}
openshift:
  url: openshift_url
source_registry:
  url: source_registry.com
"""
        config += "\n" + required_config
        config_json = read_yaml(config, 'schemas/config.json')

        expected = {
            'url': 'https://container-registry.example.com',
            'insecure': False,
            'secret': '/var/run/secrets/atomic-reactor/v2-registry-dockercfg'
        }
        conf = Configuration(raw_config=config_json)

        if exc is None:
            docker_registry = conf.docker_registry
            assert docker_registry == expected
        else:
            with pytest.raises(exc):
                getattr(conf, 'docker_registry')

    @pytest.mark.parametrize(('config', 'expected'), [
        ("""\
pull_registries: []
         """,
         []),
        ("""\
pull_registries:
- url: registry.io
         """,
         [
             {"uri": RegistryURI("registry.io"),
              "insecure": False,
              "dockercfg_path": None},
         ]),
        ("""\
pull_registries:
- url: https://registry.io
         """,
         [
             {"uri": RegistryURI("https://registry.io"),
              "insecure": False,
              "dockercfg_path": None},
         ]),
        ("""\
pull_registries:
- url: registry.io
  auth:
    cfg_path: /var/run/secrets/atomic-reactor/v2-registry-dockercfg
         """,
         [
             {"uri": RegistryURI("registry.io"),
              "insecure": False,
              "dockercfg_path": "/var/run/secrets/atomic-reactor/v2-registry-dockercfg"},
         ]),
        ("""\
pull_registries:
- url: registry.io
  insecure: true
  auth:
    cfg_path: /var/run/secrets/atomic-reactor/v2-registry-dockercfg
         """,
         [
             {"uri": RegistryURI("registry.io"),
              "insecure": True,
              "dockercfg_path": "/var/run/secrets/atomic-reactor/v2-registry-dockercfg"},
         ]),
        ("""\
pull_registries:
- url: registry.io
  insecure: true
  auth:
    cfg_path: /var/run/secrets/atomic-reactor/v2-registry-dockercfg
- url: registry.org
         """,
         [
             {"uri": RegistryURI("registry.io"),
              "insecure": True,
              "dockercfg_path": "/var/run/secrets/atomic-reactor/v2-registry-dockercfg"},
             {"uri": RegistryURI("registry.org"),
              "insecure": False,
              "dockercfg_path": None},
         ]),
    ])
    def test_get_pull_registries(self, config, expected):
        config += "\n" + REQUIRED_CONFIG

        config_json = read_yaml(config, 'schemas/config.json')
        conf = Configuration(raw_config=config_json)

        pull_registries = conf.pull_registries

        # RegistryURI does not implement equality, check URI as string
        for reg in pull_registries + expected:
            reg['uri'] = reg['uri'].uri

        assert pull_registries == expected

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
        ("""\
pull_registries:
- url: registry.io
  auth: {}
         """,
         "{!r} is a required property".format("cfg_path")),
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

    @pytest.mark.parametrize(('config', 'errors'), [
        ("""\
clusters:
  foo:
  bar: 1
  plat/form:
  - name: foo
    max_concurrent_builds: 1
        """, [
            "validation error: .clusters.foo: "
            "validating 'type' has failed "
            "(None is not of type %r)" % u'array',

            "validation error: .clusters.bar: "
            "validating 'type' has failed "
            "(1 is not of type %r)" % u'array',

            re.compile(
                "validation error: .clusters: "
                "validating 'additionalProperties' has failed"
            ),
        ]),

        ("""\
clusters:
  foo:
  - name: 1
    max_concurrent_builds: 1
  - name: blah
    max_concurrent_builds: one
  - name: "2"  # quoting prevents error
    max_concurrent_builds: 2
  - name: negative
    max_concurrent_builds: -1
        """, [
            "validation error: .clusters.foo[0].name: "
            "validating 'type' has failed "
            "(1 is not of type %r)" % u'string',

            "validation error: .clusters.foo[1].max_concurrent_builds: "
            "validating 'type' has failed "
            "('one' is not of type %r)" % u'integer',

            re.compile(r"validation error: \.clusters\.foo\[3\]\.max_concurrent_builds: "
                       r"validating 'minimum' has failed "
                       r"\(-1(\.0)? is less than the minimum of 0\)"),
        ]),

        ("""\
clusters:
  foo:
  - name: blah
    max_concurrent_builds: 1
    enabled: never
        """, [
            "validation error: .clusters.foo[0].enabled: "
            "validating 'type' has failed "
            "('never' is not of type %r)" % u'boolean',
        ]),

        ("""\
clusters:
  foo:
  # missing name
  - nam: bar
    max_concurrent_builds: 1
  # missing max_concurrent_builds
  - name: baz
    max_concurrrent_builds: 2
  - name: bar
    max_concurrent_builds: 4
    extra: false
        """, [
            "validation error: .clusters.foo[0]: validating 'required' has failed "
            "(%r is a required property)" % u'name',

            "validation error: .clusters.foo[1]: validating 'required' has failed "
            "(%r is a required property)" % u'max_concurrent_builds',

            "validation error: .clusters.foo[2]: validating 'additionalProperties' has failed "
            "(Additional properties are not allowed ('extra' was unexpected))",
        ])
    ])
    def test_bad_cluster_config(self, tmpdir, caplog, config, errors):
        config += "\n" + REQUIRED_CONFIG
        filename = os.path.join(str(tmpdir), 'config.yaml')
        with open(filename, 'w') as fp:
            fp.write(dedent(config))

        with caplog.at_level(logging.DEBUG, logger='osbs'), pytest.raises(OsbsValidationException):
            Configuration(config_path=filename)

        captured_errs = [x.message for x in caplog.records]
        for error in errors:
            try:
                # Match regexp
                assert any(filter(error.match, captured_errs))
            except AttributeError:
                # String comparison
                assert error in captured_errs

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
                registries:
                  - url: registry_url
            """))

        with pytest.raises(ValueError):
            Configuration(config_path=filename)

    @pytest.mark.parametrize(('config', 'clusters', 'defined_platforms'), [
        # Default config
        ("", [], []),

        # Unknown key
        ("""\
special: foo
        """, [], []),

        ("""\
clusters:
  all_disabled:
  - name: foo
    max_concurrent_builds: 2
    enabled: false
  platform:
  - name: one
    max_concurrent_builds: 4
  - name: two
    max_concurrent_builds: 8
    enabled: true
  - name: three
    max_concurrent_builds: 16
    enabled: false
        """, [
            ('one', 4),
            ('two', 8),
        ], ['all_disabled', 'platform']),
    ])
    def test_good_cluster_config(self, tmpdir, config, clusters, defined_platforms):
        config += "\n" + REQUIRED_CONFIG

        filename = os.path.join(str(tmpdir), 'config.yaml')
        with open(filename, 'w') as fp:
            fp.write(dedent(config))
        conf = Configuration(config_path=filename)

        enabled = conf.get_enabled_clusters_for_platform('platform')
        assert {(x.name, x.max_concurrent_builds) for x in enabled} == set(clusters)

        for platform in defined_platforms:
            assert conf.cluster_defined_for_platform(platform)

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
        'odcs', 'smtp',
        'artifacts_allowed_domains', 'yum_repo_allowed_domains', 'image_labels',
        'image_label_info_url_format', 'image_equal_labels', 'fail_on_digest_mismatch',
        'openshift', 'group_manifests', 'platform_descriptors', 'prefer_schema1_digest',
        'content_versions', 'registries', 'yum_proxy', 'source_registry', 'sources_command',
        'required_secrets', 'worker_token_secrets', 'clusters', 'hide_files',
        'skip_koji_check_for_base_image', 'deep_manifest_list_inspection'
    ])
    def test_get_methods(self, parse_from, method, tmpdir, caplog):
        if parse_from == 'raw':
            conf = Configuration(raw_config=yaml.safe_load(REACTOR_CONFIG_MAP))
        elif parse_from == 'env':
            os.environ[REACTOR_CONFIG_ENV_NAME] = dedent(REACTOR_CONFIG_MAP)
            conf = Configuration(env_name=REACTOR_CONFIG_ENV_NAME)
        elif parse_from == 'file':
            filename = str(tmpdir.join('config.yaml'))
            with open(filename, 'w') as fp:
                fp.write(dedent(REACTOR_CONFIG_MAP))
            conf = Configuration(config_path=filename)

        real_attr = getattr(conf, method)

        output = real_attr

        expected = yaml.safe_load(REACTOR_CONFIG_MAP)[method]

        if method == 'registries':
            registries_cm = {}
            for registry in expected:
                reguri = RegistryURI(registry.get('url'))
                regdict = {}
                regdict['version'] = reguri.version
                if registry.get('auth'):
                    regdict['secret'] = registry['auth']['cfg_path']
                regdict['insecure'] = registry.get('insecure', False)
                regdict['expected_media_types'] = registry.get('expected_media_types', [])

                registries_cm[reguri.docker_uri] = regdict

            assert output == registries_cm
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
build_image_override:
  ppc64le: registry.example.com/buildroot-ppc64le:latest
  arm: registry.example.com/buildroot-arm:latest
         """,
         {'ppc64le': 'registry.example.com/buildroot-ppc64le:latest',
          'arm': 'registry.example.com/buildroot-arm:latest'}),
    ])
    def test_get_build_image_override(self, config, expect):
        config += "\n" + REQUIRED_CONFIG

        config_json = read_yaml(config, 'schemas/config.json')

        conf = Configuration(raw_config=config_json)

        build_image_override = conf.build_image_override
        assert build_image_override == expect

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
registries:
  - url: registry_url
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
            'registries': [{'url': 'registry_url'}]
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

    @pytest.mark.parametrize('build_json_dir', [
        None, "/tmp/build_json_dir",
    ])
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
    def test_get_openshift_session(self, build_json_dir, config, raise_error):
        required_config = """\
version: 1
koji:
  hub_url: /
  root_url: ''
  auth: {}
source_registry:
  url: source_registry.com
registries:
  - url: registry_url
"""

        if build_json_dir:
            config += "\n  build_json_dir: " + build_json_dir
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
            'build_json_dir': build_json_dir
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

    @pytest.mark.parametrize('config, valid', [
        ("""\
build_env_vars: []
        """, True),
        ("""\
build_env_vars:
- name: HTTP_PROXY
  value: example.proxy.net
- name: NO_PROXY
  value: localhost
        """, True),
        ("""\
build_env_vars:
- name: FOO
  value: 1
        """, False),  # values must be strings
        ("""\
build_env_vars:
- name: FOO
        """, False),  # values must be defined
    ])
    def test_validate_build_env_vars(self, config, valid):
        # Only test schema validation, atomic-reactor has no additional support
        # for build_env_vars (osbs-client does, however)
        config += "\n" + REQUIRED_CONFIG
        if valid:
            read_yaml(config, 'schemas/config.json')
        else:
            with pytest.raises(OsbsValidationException):
                read_yaml(config, 'schemas/config.json')

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
