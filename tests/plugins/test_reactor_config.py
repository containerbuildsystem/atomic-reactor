"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals

from jsonschema import ValidationError
import io
import logging
import os
import pkg_resources
import pytest
from textwrap import dedent
import re
import yaml
import smtplib
from copy import deepcopy

try:
    import pdc_client
    PDC_AVAILABLE = True
except ImportError:
    PDC_AVAILABLE = False

import atomic_reactor
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.util import read_yaml
import atomic_reactor.koji_util
import atomic_reactor.pulp_util
import atomic_reactor.odcs_util
import osbs.conf
import osbs.api
from osbs.utils import RegistryURI
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfig,
                                                       ReactorConfigPlugin,
                                                       get_config, WORKSPACE_CONF_KEY,
                                                       get_koji_session,
                                                       get_pulp_session,
                                                       get_odcs_session,
                                                       get_smtp_session,
                                                       get_pdc_session,
                                                       get_openshift_session)
from tests.constants import TEST_IMAGE, REACTOR_CONFIG_MAP
from tests.docker_mock import mock_docker
from tests.fixtures import reactor_config_map  # noqa
from flexmock import flexmock


class TestReactorConfigPlugin(object):
    def prepare(self):
        mock_docker()
        tasker = DockerTasker()
        workflow = DockerBuildWorkflow({'provider': 'git', 'uri': 'asd'},
                                       TEST_IMAGE)
        return tasker, workflow

    def test_no_config(self):
        tasker, workflow = self.prepare()
        conf = get_config(workflow)
        assert isinstance(conf, ReactorConfig)

        same_conf = get_config(workflow)
        assert conf is same_conf

    @pytest.mark.parametrize('basename', ['reactor-config.yaml', None])
    def test_filename(self, tmpdir, basename):
        filename = os.path.join(str(tmpdir), basename or 'config.yaml')
        with open(filename, 'w'):
            pass

        tasker, workflow = self.prepare()
        plugin = ReactorConfigPlugin(tasker, workflow,
                                     config_path=str(tmpdir),
                                     basename=filename)
        assert plugin.run() is None

    def test_filename_not_found(self):
        tasker, workflow = self.prepare()
        plugin = ReactorConfigPlugin(tasker, workflow, config_path='/not-found')
        with pytest.raises(Exception):
            plugin.run()

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

        tasker, workflow = self.prepare()
        plugin = ReactorConfigPlugin(tasker, workflow, config_path=str(tmpdir))
        with caplog.atLevel(logging.ERROR), pytest.raises(Exception):
            plugin.run()

        captured_errs = [x.message for x in caplog.records()]
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

        tasker, workflow = self.prepare()
        plugin = ReactorConfigPlugin(tasker, workflow, config_path=str(tmpdir))
        with caplog.atLevel(logging.ERROR), pytest.raises(Exception):
            plugin.run()

        captured_errs = [x.message for x in caplog.records()]
        assert any("cannot validate" in x for x in captured_errs)

    @pytest.mark.parametrize(('config', 'errors'), [
        ("""\
          clusters:
            foo:
            - name: bar
              max_concurrent_builds: 1
        """, [
            "validation error (at top level): "
            "%r is a required property" % u'version',
        ]),

        ("""\
          version: 1
          clusters:
            foo:
            bar: 1
            plat/form:
            - name: foo
              max_concurrent_builds: 1
        """, [
            "validation error (clusters.foo): None is not of type %r" % u'array',

            "validation error (clusters.bar): 1 is not of type %r" % u'array',

            re.compile(r"validation error \(clusters\): .*'plat/form'"),
        ]),

        ("""\
          version: 1
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
            "validation error (clusters.foo[0].name): "
            "1 is not of type %r" % u'string',

            "validation error (clusters.foo[1].max_concurrent_builds): "
            "'one' is not of type %r" % u'integer',

            "validation error (clusters.foo[3].max_concurrent_builds): "
            "-1 is less than the minimum of 0",
        ]),

        ("""\
          version: 1
          clusters:
            foo:
            - name: blah
              max_concurrent_builds: 1
              enabled: never
        """, [
            "validation error (clusters.foo[0].enabled): "
            "'never' is not of type %r" % u'boolean',
        ]),

        ("""\
          version: 1
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
            "validation error (clusters.foo[0]): "
            "%r is a required property" % u'name',

            "validation error (clusters.foo[1]): "
            "%r is a required property" % u'max_concurrent_builds',

            "validation error (clusters.foo[2]): "
            "Additional properties are not allowed ('extra' was unexpected)",
        ])
    ])
    def test_bad_cluster_config(self, tmpdir, caplog, reactor_config_map,
                                config, errors):
        if reactor_config_map:
            os.environ['REACTOR_CONFIG'] = dedent(config)
        else:
            filename = os.path.join(str(tmpdir), 'config.yaml')
            with open(filename, 'w') as fp:
                fp.write(dedent(config))
        tasker, workflow = self.prepare()
        plugin = ReactorConfigPlugin(tasker, workflow, config_path=str(tmpdir))

        with caplog.atLevel(logging.ERROR), pytest.raises(ValidationError):
            plugin.run()

        os.environ.pop('REACTOR_CONFIG', None)
        captured_errs = [x.message for x in caplog.records()]
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
            fp.write("version: 2")
        tasker, workflow = self.prepare()
        plugin = ReactorConfigPlugin(tasker, workflow, config_path=str(tmpdir))

        with pytest.raises(ValueError):
            plugin.run()

    @pytest.mark.parametrize(('config', 'clusters'), [
        # Empty config
        ("", []),

        # Built-in default config
        (yaml.dump(ReactorConfig.DEFAULT_CONFIG), []),

        # Unknown key
        ("""\
          version: 1
          special: foo
        """, []),

        ("""\
          version: 1
          clusters:
            ignored:
            - name: foo
              max_concurrent_builds: 2
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
        ]),
    ])
    def test_good_cluster_config(self, tmpdir, reactor_config_map, config, clusters):
        if reactor_config_map and config:
            os.environ['REACTOR_CONFIG'] = dedent(config)
        else:
            filename = os.path.join(str(tmpdir), 'config.yaml')
            with open(filename, 'w') as fp:
                fp.write(dedent(config))
        tasker, workflow = self.prepare()
        plugin = ReactorConfigPlugin(tasker, workflow, config_path=str(tmpdir))
        assert plugin.run() is None
        os.environ.pop('REACTOR_CONFIG', None)

        conf = get_config(workflow)
        enabled = conf.get_enabled_clusters_for_platform('platform')
        assert set([(x.name, x.max_concurrent_builds)
                    for x in enabled]) == set(clusters)

    @pytest.mark.parametrize('default', (
        'release',
        'beta',
        'unsigned',
    ))
    def test_odcs_config(self, tmpdir, default):
        filename = str(tmpdir.join('config.yaml'))
        with open(filename, 'w') as fp:
            fp.write(dedent("""\
                version: 1
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
                """.format(default=default)))

        tasker, workflow = self.prepare()
        plugin = ReactorConfigPlugin(tasker, workflow, config_path=str(tmpdir))
        assert plugin.run() is None

        odcs_config = get_config(workflow).get_odcs_config()

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
        filename = str(tmpdir.join('config.yaml'))
        with open(filename, 'w') as fp:
            fp.write(dedent("""\
                version: 1
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
                """))

        tasker, workflow = self.prepare()
        plugin = ReactorConfigPlugin(tasker, workflow, config_path=str(tmpdir))
        assert plugin.run() is None

        with pytest.raises(ValueError) as exc_info:
            get_config(workflow).get_odcs_config()
        assert 'unknown signing intent' in str(exc_info.value)

    @pytest.mark.parametrize('fallback', (True, False, None))
    @pytest.mark.parametrize('method', [
        'koji', 'pulp', 'odcs', 'smtp', 'pdc', 'arrangement_version',
        'artifacts_allowed_domains', 'image_labels', 'image_equal_labels',
        'openshift', 'group_manifests', 'platform_descriptors', 'prefer_schema1_digest',
        'content_versions', 'registries', 'yum_proxy', 'source_registry', 'sources_command',
        'required_secrets', 'worker_token_secrets', 'clusters',
    ])
    def test_get_methods(self, fallback, method):
        tasker, workflow = self.prepare()
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
        if fallback is False:
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] = \
                 ReactorConfig(yaml.safe_load(REACTOR_CONFIG_MAP))
        else:
            if fallback:
                fall_source = ReactorConfig(yaml.safe_load(REACTOR_CONFIG_MAP))
            else:
                fall_source = ReactorConfig(yaml.safe_load("version: 1"))

        method_name = 'get_' + method
        real_method = getattr(atomic_reactor.plugins.pre_reactor_config, method_name)

        if fallback is True:
            output = real_method(workflow, fall_source.conf[method])
        else:
            if fallback is False:
                output = real_method(workflow)
            else:
                with pytest.raises(KeyError):
                    real_method(workflow)
                return

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

                registries_cm[reguri.docker_uri] = regdict

            if fallback:
                output = real_method(workflow, registries_cm)
            assert output == registries_cm
            return

        if method == 'source_registry':
            expect = {
                'uri': RegistryURI(expected['url']),
                'insecure': expected.get('insecure', False)
            }
            if fallback:
                output = real_method(workflow, expect)
            assert output['insecure'] == expect['insecure']
            assert output['uri'].uri == expect['uri'].uri
            return

        assert output == expected

    @pytest.mark.parametrize('fallback', (True, False))
    @pytest.mark.parametrize(('config', 'raise_error'), [
        ("""\
          version: 1
          koji:
              hub_url: https://koji.example.com/hub
              root_url: https://koji.example.com/root
              auth:
                  proxyuser: proxyuser
                  krb_principal: krb_principal
                  krb_keytab_path: /tmp/krb_keytab
        """, False),

        ("""\
          version: 1
          koji:
              hub_url: https://koji.example.com/hub
              root_url: https://koji.example.com/root
              auth:
                  proxyuser: proxyuser
                  ssl_certs_dir: /var/certs
        """, False),

        ("""\
          version: 1
          koji:
              hub_url: https://koji.example.com/hub
              root_url: https://koji.example.com/root
              auth:
                  proxyuser: proxyuser
        """, False),

        ("""\
          version: 1
          koji:
              hub_url: https://koji.example.com/hub
              root_url: https://koji.example.com/root
              auth:
        """, True),

        ("""\
          version: 1
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
          version: 1
          koji:
              hub_url: https://koji.example.com/hub
              root_url: https://koji.example.com/root
              auth:
                  proxyuser: proxyuser
                  krb_keytab_path: /tmp/krb_keytab
        """, True),

        ("""\
          version: 1
          koji:
              hub_url: https://koji.example.com/hub
              root_url: https://koji.example.com/root
              auth:
                  proxyuser: proxyuser
                  krb_principal: krb_principal
        """, True),

        ("""\
          version: 1
          koji:
              hub_url: https://koji.example.com/hub
              root_url: https://koji.example.com/root
              auth:
                  proxyuser: proxyuser
                  krb_principal: krb_principal
                  ssl_certs_dir: /var/certs
        """, True),

        ("""\
          version: 1
          koji:
              hub_url: https://koji.example.com/hub
              root_url: https://koji.example.com/root
              auth:
                  proxyuser: proxyuser
                  krb_keytab_path: /tmp/krb_keytab
                  ssl_certs_dir: /var/certs
        """, True),
    ])
    def test_get_koji_session(self, fallback, config, raise_error):
        tasker, workflow = self.prepare()
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

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

        fallback_map = {}
        if fallback:
            fallback_map = {'auth': deepcopy(auth_info), 'hub_url': config_json['koji']['hub_url']}
            fallback_map['auth']['krb_keytab_path'] = fallback_map['auth'].pop('krb_keytab')
        else:
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] = \
                ReactorConfig(config_json)

        (flexmock(atomic_reactor.koji_util)
            .should_receive('create_koji_session')
            .with_args(config_json['koji']['hub_url'], auth_info)
            .once()
            .and_return(True))

        get_koji_session(workflow, fallback_map)

    @pytest.mark.parametrize('fallback', (True, False))
    @pytest.mark.parametrize(('config', 'raise_error'), [
        ("""\
          version: 1
          pulp:
              name: my-pulp
              auth:
                  password: testpasswd
                  username: testuser
        """, False),

        ("""\
          version: 1
          pulp:
              name: my-pulp
              auth:
                  ssl_certs_dir: /var/certs
        """, False),

        ("""\
          version: 1
          pulp:
              name: my-pulp
              auth:
                  ssl_certs_dir: /var/certs
                  password: testpasswd
                  username: testuser
        """, True),


        ("""\
          version: 1
          pulp:
              name: my-pulp
              auth:
                  ssl_certs_dir: /var/certs
                  password: testpasswd
        """, True),

        ("""\
          version: 1
          pulp:
              name: my-pulp
              auth:
                  ssl_certs_dir: /var/certs
                  username: testuser
        """, True),

        ("""\
          version: 1
          pulp:
              name: my-pulp
              auth:
                  username: testuser
        """, True),

        ("""\
          version: 1
          pulp:
              name: my-pulp
              auth:
                  password: testpasswd
        """, True),
    ])
    def test_get_pulp_session(self, fallback, config, raise_error):
        tasker, workflow = self.prepare()
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

        if raise_error:
            with pytest.raises(Exception):
                read_yaml(config, 'schemas/config.json')
            return
        config_json = read_yaml(config, 'schemas/config.json')

        auth_info = {
            "pulp_secret_path": config_json['pulp']['auth'].get('ssl_certs_dir'),
            "username": config_json['pulp']['auth'].get('username'),
            "password": config_json['pulp']['auth'].get('password'),
            "dockpulp_loglevel": None
        }

        fallback_map = {}
        if fallback:
            fallback_map = {'auth': deepcopy(auth_info), 'name': config_json['pulp']['name']}
            fallback_map['auth']['ssl_certs_dir'] = fallback_map['auth'].pop('pulp_secret_path')
        else:
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig(config_json)

        (flexmock(atomic_reactor.pulp_util.PulpHandler)
            .should_receive('__init__')
            .with_args(workflow, config_json['pulp']['name'], 'logger', **auth_info)
            .once()
            .and_return(None))

        get_pulp_session(workflow, 'logger', fallback_map)

    @pytest.mark.parametrize('fallback', (True, False))
    @pytest.mark.parametrize(('config', 'raise_error'), [
        ("""\
          version: 1
          odcs:
              api_url: https://odcs.example.com/api/1
              auth:
                  ssl_certs_dir: /var/run/secrets/atomic-reactor/odcssecret
              signing_intents:
              - name: release
                keys: [R123]
              default_signing_intent: default
        """, False),

        ("""\
          version: 1
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
          version: 1
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
          version: 1
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
          version: 1
          odcs:
              api_url: https://odcs.example.com/api/1
              auth:
                  openidc_dir: /var/run/open_idc
              signing_intents:
              - name: release
                keys: [R123]
        """, True),

        ("""\
          version: 1
          odcs:
              api_url: https://odcs.example.com/api/1
              auth:
                  openidc_dir: /var/run/open_idc
              default_signing_intent: default
        """, True),

        ("""\
          version: 1
          odcs:
              auth:
                  openidc_dir: /var/run/open_idc
              signing_intents:
              - name: release
                keys: [R123]
              default_signing_intent: default
        """, True),
    ])
    def test_get_odcs_session(self, tmpdir, fallback, config, raise_error):
        tasker, workflow = self.prepare()
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

        if raise_error:
            with pytest.raises(Exception):
                read_yaml(config, 'schemas/config.json')
            return
        config_json = read_yaml(config, 'schemas/config.json')

        auth_info = {'insecure': config_json['odcs'].get('insecure', False)}
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

        fallback_map = {}
        if fallback:
            fallback_map = {'auth': deepcopy(auth_info),
                            'api_url': config_json['odcs']['api_url']}
            fallback_map['auth']['ssl_certs_dir'] = config_json['odcs']['auth'].get('ssl_certs_dir')
            fallback_map['auth']['openidc_dir'] = config_json['odcs']['auth'].get('openidc_dir')
        else:
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig(config_json)

        if not ssl_dir_raise:
            (flexmock(atomic_reactor.odcs_util.ODCSClient)
                .should_receive('__init__')
                .with_args(config_json['odcs']['api_url'], **auth_info)
                .once()
                .and_return(None))

            get_odcs_session(workflow, fallback_map)
        else:
            with pytest.raises(KeyError):
                get_odcs_session(workflow, fallback_map)

    @pytest.mark.parametrize('fallback', (True, False))
    @pytest.mark.parametrize(('config', 'raise_error'), [
        ("""\
          version: 1
          smtp:
              host: smtp.example.com
              from_address: osbs@example.com
        """, False),

        ("""\
          version: 1
          smtp:
              from_address: osbs@example.com
        """, True),

        ("""\
          version: 1
          smtp:
              host: smtp.example.com
        """, True),

        ("""\
          version: 1
          smtp:
        """, True),
    ])
    def test_get_smtp_session(self, fallback, config, raise_error):
        tasker, workflow = self.prepare()
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

        if raise_error:
            with pytest.raises(Exception):
                read_yaml(config, 'schemas/config.json')
            return
        config_json = read_yaml(config, 'schemas/config.json')

        fallback_map = {}
        if fallback:
            fallback_map['host'] = config_json['smtp']['host']
        else:
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig(config_json)

        (flexmock(smtplib.SMTP)
            .should_receive('__init__')
            .with_args(config_json['smtp']['host'])
            .once()
            .and_return(None))

        get_smtp_session(workflow, fallback_map)

    @pytest.mark.parametrize('fallback', (True, False))
    @pytest.mark.parametrize(('config', 'raise_error'), [
        ("""\
          version: 1
          pdc:
             api_url: https://pdc.example.com/rest_api/v1
        """, False),

        ("""\
          version: 1
          pdc:
        """, True),
    ])
    def test_get_pdc_session(self, fallback, config, raise_error):
        tasker, workflow = self.prepare()
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

        if raise_error:
            with pytest.raises(Exception):
                read_yaml(config, 'schemas/config.json')
            return
        config_json = read_yaml(config, 'schemas/config.json')

        if not PDC_AVAILABLE:
            return

        auth_info = {
            "server": config_json['pdc']['api_url'],
            "ssl_verify": not config_json['pdc'].get('insecure', False),
            "develop": True,
        }

        fallback_map = {}
        if fallback:
            fallback_map['api_url'] = config_json['pdc']['api_url']
            fallback_map['insecure'] = config_json['pdc'].get('insecure', False)
        else:
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig(config_json)

        (flexmock(pdc_client.PDCClient)
            .should_receive('__init__')
            .with_args(**auth_info)
            .once()
            .and_return(None))

        get_pdc_session(workflow, fallback_map)

    @pytest.mark.parametrize('fallback', (True, False))
    @pytest.mark.parametrize('build_json_dir', [
        None, "/tmp/build_json_dir",
    ])
    @pytest.mark.parametrize(('config', 'raise_error'), [
        ("""\
          version: 1
          openshift:
              url: https://openshift.example.com
              auth:
                  ssl_certs_dir: /var/run/secrets/atomic-reactor/odcssecret
        """, False),

        ("""\
          version: 1
          openshift:
              url: https://openshift.example.com
        """, False),

        ("""\
          version: 1
          openshift:
              url: https://openshift.example.com
              auth:
                  krb_principal: principal
                  krb_keytab_path: /var/keytab
        """, False),

        ("""\
          version: 1
          openshift:
              url: https://openshift.example.com
              auth:
                  krb_principal: principal
                  krb_keytab_path: /var/keytab
                  krb_cache_path: /var/krb/cache
        """, False),

        ("""\
          version: 1
          openshift:
              url: https://openshift.example.com
              auth:
                  enable: True
        """, False),

        ("""\
          version: 1
          openshift:
              url: https://openshift.example.com
              auth:
                  krb_keytab_path: /var/keytab
        """, True),

        ("""\
          version: 1
          openshift:
              url: https://openshift.example.com
              auth:
                  krb_principal: principal
        """, True),

        ("""\
          version: 1
          openshift:
              auth:
                  ssl_certs_dir: /var/run/secrets/atomic-reactor/odcssecret
        """, True),

        ("""\
          version: 1
          openshift:
              auth:
                  krb_principal: principal
                  krb_keytab_path: /var/keytab
        """, True),

        ("""\
          version: 1
          openshift:
              url: https://openshift.example.com
              auth:
        """, True),

        ("""\
          version: 1
          openshift:
              auth:
                  ssl_certs_dir: /var/run/secrets/atomic-reactor/odcssecret
        """, True),
    ])
    def test_get_openshift_session(self, fallback, build_json_dir, config, raise_error):
        tasker, workflow = self.prepare()
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

        if build_json_dir:
            config += "      build_json_dir: " + build_json_dir

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

        fallback_map = {}
        if fallback:
            fallback_map = {'url': config_json['openshift']['url'],
                            'insecure': config_json['openshift'].get('insecure', False),
                            'build_json_dir': build_json_dir}
            if config_json['openshift'].get('auth'):
                fallback_map['auth'] = {}
                fallback_map['auth']['krb_keytab_path'] =\
                    config_json['openshift']['auth'].get('krb_keytab_path')
                fallback_map['auth']['krb_principal'] =\
                    config_json['openshift']['auth'].get('krb_principal')

                fallback_map['auth']['enable'] =\
                    config_json['openshift']['auth'].get('enable', False)
                fallback_map['auth']['krb_cache_path'] =\
                    config_json['openshift']['auth'].get('krb_cache_path')
                fallback_map['auth']['ssl_certs_dir'] =\
                    config_json['openshift']['auth'].get('ssl_certs_dir')
        else:
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig(config_json)

        (flexmock(osbs.conf.Configuration)
            .should_call('__init__')
            .with_args(**auth_info)
            .once())
        (flexmock(osbs.api.OSBS)
            .should_call('__init__')
            .once())
        flexmock(os, environ={'BUILD': '{"metadata": {"namespace": "namespace"}}'})

        get_openshift_session(workflow, fallback_map)
