"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals, absolute_import

import io
import json
import logging
import os
import pkg_resources
import pytest
from textwrap import dedent
import re
import yaml
import smtplib
from copy import deepcopy

import atomic_reactor
import koji
from atomic_reactor.core import ContainerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.util import read_yaml
import atomic_reactor.utils.cachito
import atomic_reactor.utils.koji
import atomic_reactor.utils.odcs
import osbs.conf
import osbs.api
from osbs.utils import RegistryURI
from osbs.exceptions import OsbsValidationException
from atomic_reactor.plugins.pre_reactor_config import (ODCSConfig,
                                                       ReactorConfig,
                                                       ReactorConfigPlugin,
                                                       get_config, WORKSPACE_CONF_KEY,
                                                       get_koji_session,
                                                       get_koji_path_info,
                                                       get_odcs_session,
                                                       get_smtp_session,
                                                       get_openshift_session,
                                                       get_clusters_client_config_path,
                                                       get_docker_registry,
                                                       get_pull_registries,
                                                       get_platform_to_goarch_mapping,
                                                       get_goarch_to_platform_mapping,
                                                       get_default_image_build_method,
                                                       get_buildstep_alias,
                                                       get_flatpak_base_image,
                                                       get_flatpak_metadata,
                                                       get_cachito_session,
                                                       get_operator_manifests,
                                                       CONTAINER_DEFAULT_BUILD_METHOD,
                                                       get_build_image_override,
                                                       NO_FALLBACK)
from tests.constants import TEST_IMAGE, REACTOR_CONFIG_MAP
from tests.docker_mock import mock_docker
from tests.stubs import StubInsideBuilder
from flexmock import flexmock


USER_PARAMS = {'git_uri': 'test_uri', 'git_ref': 'test_ref', 'git_breanch': 'test_branch'}


class TestReactorConfigPlugin(object):
    def prepare(self):
        mock_docker()
        tasker = ContainerTasker()
        workflow = DockerBuildWorkflow(
            TEST_IMAGE,
            source={'provider': 'git', 'uri': 'asd'},
        )
        workflow.builder = StubInsideBuilder()
        workflow.builder.tasker = tasker

        os.environ['USER_PARAMS'] = json.dumps(USER_PARAMS)
        return tasker, workflow

    @pytest.mark.parametrize(('fallback'), [
        False,
        True
    ])
    @pytest.mark.parametrize(('config', 'valid'), [
        ("""\
            version: 1
            registries:
            - url: https://container-registry.example.com/v2
              auth:
                  cfg_path: /var/run/secrets/atomic-reactor/v2-registry-dockercfg
         """,
         True),
        ("""\
            version: 1
            registries:
            - url: https://container-registry.example.com/v2
              auth:
                  cfg_path: /var/run/secrets/atomic-reactor/v2-registry-dockercfg
            - url: https://another-container-registry.example.com/
              auth:
                  cfg_path: /var/run/secrets/atomic-reactor/another-registry-dockercfg
         """,
         True),
        ("""\
            version: 1
            registries:
            - url: https://old-container-registry.example.com/v1
              auth:
                  cfg_path: /var/run/secrets/atomic-reactor/v1-registry-dockercfg
         """,
         False),
    ])
    def test_get_docker_registry(self, config, fallback, valid):
        _, workflow = self.prepare()
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

        config_json = read_yaml(config, 'schemas/config.json')

        docker_reg = {
            'version': 'v2',
            'insecure': False,
            'secret': '/var/run/secrets/atomic-reactor/v2-registry-dockercfg',
            'url': 'https://container-registry.example.com/v2',
        }

        if fallback:
            if valid:
                docker_fallback = docker_reg
                expected = docker_reg
            else:
                docker_fallback = NO_FALLBACK
        else:
            docker_fallback = {}
            expected = {
                'url': 'https://container-registry.example.com',
                'insecure': False,
                'secret': '/var/run/secrets/atomic-reactor/v2-registry-dockercfg'
            }
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig(config_json)

        if valid:
            docker_registry = get_docker_registry(workflow, docker_fallback)
            assert docker_registry == expected
        else:
            if fallback:
                with pytest.raises(KeyError):
                    get_docker_registry(workflow, docker_fallback)
            else:
                with pytest.raises(OsbsValidationException):
                    get_docker_registry(workflow, docker_fallback)

    @pytest.mark.parametrize(('config', 'expected'), [
        ("""\
            version: 1
            pull_registries: []
         """,
         []),
        ("""\
            version: 1
            pull_registries:
            - url: registry.io
         """,
         [
             {"uri": RegistryURI("registry.io"),
              "insecure": False,
              "dockercfg_path": None},
         ]),
        ("""\
            version: 1
            pull_registries:
            - url: https://registry.io
         """,
         [
             {"uri": RegistryURI("https://registry.io"),
              "insecure": False,
              "dockercfg_path": None},
         ]),
        ("""\
            version: 1
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
            version: 1
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
            version: 1
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
        _, workflow = self.prepare()
        reactor_conf = workflow.plugin_workspace.setdefault(ReactorConfigPlugin.key, {})

        config_json = read_yaml(config, 'schemas/config.json')
        reactor_conf[WORKSPACE_CONF_KEY] = ReactorConfig(config_json)

        pull_registries = get_pull_registries(workflow)

        # RegistryURI does not implement equality, check URI as string
        for reg in pull_registries + expected:
            reg['uri'] = reg['uri'].uri

        assert pull_registries == expected

    @pytest.mark.parametrize('config, error', [
        ("""\
             version: 1
             pull_registries: {}
         """,
         "is not of type {!r}".format("array")),
        ("""\
             version: 1
             pull_registries:
             - insecure: false
         """,
         "{!r} is a required property".format("url")),
        ("""\
             version: 1
             pull_registries:
             - url: registry.io
               auth: {}
         """,
         "{!r} is a required property".format("cfg_path")),
    ])
    def test_get_pull_registries_schema_validation(self, config, error):
        with pytest.raises(OsbsValidationException) as exc_info:
            read_yaml(config, 'schemas/config.json')
        assert error in str(exc_info.value)

    def test_no_config(self):
        _, workflow = self.prepare()
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
        os.environ.pop('REACTOR_CONFIG', None)
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
        with caplog.at_level(logging.ERROR), pytest.raises(Exception):
            plugin.run()

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

        tasker, workflow = self.prepare()
        plugin = ReactorConfigPlugin(tasker, workflow, config_path=str(tmpdir))
        with caplog.at_level(logging.ERROR), pytest.raises(Exception):
            plugin.run()

        captured_errs = [x.message for x in caplog.records]
        assert any("cannot validate" in x for x in captured_errs)

    @pytest.mark.parametrize(('config', 'errors'), [
        ("""\
          clusters:
            foo:
            - name: bar
              max_concurrent_builds: 1
        """, [
            "validation error: at top level: "
            "validating 'required' has failed "
            "(%r is a required property)" % u'version',
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
            "validation error: .clusters.foo: "
            "validating 'type' has failed "
            "(None is not of type %r)" % u'array',

            "validation error: .clusters.bar: "
            "validating 'type' has failed "
            "(1 is not of type %r)" % u'array',

            "validation error: .clusters: "
            "validating 'additionalProperties' has failed "
            "(Additional properties are not allowed ('plat/form' was unexpected))",
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
          version: 1
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
            "validation error: .clusters.foo[0]: validating 'required' has failed "
            "(%r is a required property)" % u'name',

            "validation error: .clusters.foo[1]: validating 'required' has failed "
            "(%r is a required property)" % u'max_concurrent_builds',

            "validation error: .clusters.foo[2]: validating 'additionalProperties' has failed "
            "(Additional properties are not allowed ('extra' was unexpected))",
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

        with caplog.at_level(logging.ERROR), pytest.raises(OsbsValidationException):
            plugin.run()

        os.environ.pop('REACTOR_CONFIG', None)
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

    @pytest.mark.parametrize(('extra_config', 'fallback', 'error'), [
        ('clusters_client_config_dir: /the/path', None, None),
        ('clusters_client_config_dir: /the/path', '/unused/path', None),
        (None, '/the/path', None),
        (None, NO_FALLBACK, KeyError),
    ])
    def test_cluster_client_config_path(self, tmpdir, reactor_config_map, extra_config, fallback,
                                        error):
        config = 'version: 1'
        if extra_config:
            config += '\n' + extra_config
        if reactor_config_map and config:
            os.environ['REACTOR_CONFIG'] = config
        else:
            filename = os.path.join(str(tmpdir), 'config.yaml')
            with open(filename, 'w') as fp:
                fp.write(config)
        tasker, workflow = self.prepare()
        plugin = ReactorConfigPlugin(tasker, workflow, config_path=str(tmpdir))
        assert plugin.run() is None
        os.environ.pop('REACTOR_CONFIG', None)

        if error:
            with pytest.raises(error):
                get_clusters_client_config_path(workflow, fallback)
        else:
            path = get_clusters_client_config_path(workflow, fallback)
            assert path == '/the/path/osbs.conf'

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
        message = str(exc_info.value)
        assert message == dedent("""\
            unknown signing intent name "spam", valid names: unsigned, beta, release
            """.rstrip())

    def test_odcs_config_deprecated_signing_intent(self, tmpdir, caplog):
        filename = str(tmpdir.join('config.yaml'))
        with open(filename, 'w') as fp:
            fp.write(dedent("""\
                version: 1
                odcs:
                   signing_intents:
                   - name: release
                     keys: [R123]
                     deprecated_keys: [R122]
                   default_signing_intent: release
                   api_url: http://odcs.example.com
                   auth:
                       ssl_certs_dir: /var/run/secrets/atomic-reactor/odcssecret
                """))

        tasker, workflow = self.prepare()
        plugin = ReactorConfigPlugin(tasker, workflow, config_path=str(tmpdir))
        assert plugin.run() is None

        odcs_config = get_config(workflow).get_odcs_config()
        signing_intent = odcs_config.get_signing_intent_by_keys(['R123'])
        assert signing_intent['name'] == 'release'
        assert 'contain deprecated entries' not in caplog.text

        signing_intent = odcs_config.get_signing_intent_by_keys(['R123', 'R122'])
        assert signing_intent['name'] == 'release'
        assert 'contain deprecated entries' in caplog.text

    @pytest.mark.parametrize('fallback', (True, False, None))
    @pytest.mark.parametrize('method', [
        'koji', 'odcs', 'smtp', 'arrangement_version',
        'artifacts_allowed_domains', 'yum_repo_allowed_domains', 'image_labels',
        'image_label_info_url_format', 'image_equal_labels', 'fail_on_digest_mismatch',
        'openshift', 'group_manifests', 'platform_descriptors', 'prefer_schema1_digest',
        'content_versions', 'registries', 'yum_proxy', 'source_registry', 'sources_command',
        'required_secrets', 'worker_token_secrets', 'clusters', 'hide_files',
        'skip_koji_check_for_base_image', 'deep_manifest_list_inspection'
    ])
    def test_get_methods(self, fallback, method):
        _, workflow = self.prepare()
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
                regdict['expected_media_types'] = registry.get('expected_media_types', [])

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
    @pytest.mark.parametrize(('config', 'expect'), [
        ("""\
          version: 1
          platform_descriptors:
            - platform: x86_64
              architecture: amd64
         """,
         {'x86_64': 'amd64',
          'ppc64le': 'ppc64le'}),
    ])
    def test_get_platform_to_goarch_mapping(self, fallback, config, expect):
        _, workflow = self.prepare()
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

        config_json = read_yaml(config, 'schemas/config.json')

        workspace = workflow.plugin_workspace[ReactorConfigPlugin.key]
        workspace[WORKSPACE_CONF_KEY] = ReactorConfig(config_json)

        kwargs = {}
        if fallback:
            kwargs['descriptors_fallback'] = {'x86_64': 'amd64'}
        platform_to_goarch = get_platform_to_goarch_mapping(workflow, **kwargs)
        goarch_to_platform = get_goarch_to_platform_mapping(workflow, **kwargs)
        for plat, goarch in expect.items():
            assert platform_to_goarch[plat] == goarch
            assert goarch_to_platform[goarch] == plat

    @pytest.mark.parametrize(('config', 'expect'), [
        ("""\
          version: 1
          default_image_build_method: buildah_bud
         """,
         "buildah_bud"),
        ("""\
          version: 1
          default_image_build_method: imagebuilder
         """,
         "imagebuilder"),
        ("""\
          version: 1
         """,
         CONTAINER_DEFAULT_BUILD_METHOD),
    ])
    def test_get_default_image_build_method(self, config, expect):
        config_json = read_yaml(config, 'schemas/config.json')
        _, workflow = self.prepare()
        workspace = workflow.plugin_workspace.setdefault(ReactorConfigPlugin.key, {})
        workspace[WORKSPACE_CONF_KEY] = ReactorConfig(config_json)

        method = get_default_image_build_method(workflow)
        assert method == expect

    @pytest.mark.parametrize(('config', 'expect'), [
        ("""\
          version: 1
          buildstep_alias:
            docker_api: imagebuilder
         """,
         {'docker_api': 'imagebuilder'}),
        ("""\
          version: 1
          buildstep_alias:
            another: docker_api
         """,
         {'another': 'docker_api'}),
        ("""\
          version: 1
          buildstep_alias:
            docker_api: imagebuilder
            another: imagebuilder
         """,
         {'docker_api': 'imagebuilder',
          'another': 'imagebuilder'}),
        ("""\
          version: 1
         """,
         {}),
    ])
    def test_get_buildstep_alias(self, config, expect):
        config_json = read_yaml(config, 'schemas/config.json')
        _, workflow = self.prepare()
        workspace = workflow.plugin_workspace.setdefault(ReactorConfigPlugin.key, {})
        workspace[WORKSPACE_CONF_KEY] = ReactorConfig(config_json)

        method = get_buildstep_alias(workflow)
        assert method == expect

    @pytest.mark.parametrize(('config', 'source_buildstep', 'expect_source', 'expect_default',
                              'will_raise'), [
        ("""\
          version: 1
          default_image_build_method: docker_api
          buildstep_alias:
            docker_api: imagebuilder
         """,
         None, None, 'imagebuilder', False),
        ("""\
          version: 1
          buildstep_alias:
            docker_api: imagebuilder
         """,
         'docker_api', 'imagebuilder', 'imagebuilder', False),
        ("""\
          version: 1
          default_image_build_method: buildah_bud
         """,
         None, None, None, True),
        ("""\
          version: 1
          default_image_build_method: docker_api
         """,
         'buildah_bud', None, None, True),
    ])
    def test_get_buildstep_alias_setting(self, tmpdir, config, source_buildstep,
                                         expect_source, expect_default, will_raise):
        filename = os.path.join(str(tmpdir), 'config.yaml')
        with open(filename, 'w') as fp:
            fp.write(dedent(config))
        tasker, workflow = self.prepare()
        workflow.builder.source.config.image_build_method = source_buildstep
        plugin = ReactorConfigPlugin(tasker, workflow, config_path=str(tmpdir))

        if will_raise:
            with pytest.raises(NotImplementedError):
                plugin.run()
            return

        assert plugin.run() is None
        assert workflow.builder.source.config.image_build_method == expect_source
        assert workflow.default_image_build_method == expect_default
        assert workflow.builder.tasker.build_method == expect_source or expect_default

    @pytest.mark.parametrize('fallback', (True, False))
    @pytest.mark.parametrize(('config', 'expect'), [
        ("""\
          version: 1
          build_image_override:
            ppc64le: registry.example.com/buildroot-ppc64le:latest
            arm: registry.example.com/buildroot-arm:latest
         """,
         {'ppc64le': 'registry.example.com/buildroot-ppc64le:latest',
          'arm': 'registry.example.com/buildroot-arm:latest'}),
    ])
    def test_get_build_image_override(self, fallback, config, expect):
        _, workflow = self.prepare()
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

        config_json = read_yaml(config, 'schemas/config.json')

        workspace = workflow.plugin_workspace[ReactorConfigPlugin.key]
        workspace[WORKSPACE_CONF_KEY] = ReactorConfig(config_json)

        kwargs = {}
        if fallback:
            kwargs['fallback'] = expect
        build_image_override = get_build_image_override(workflow, **kwargs)
        assert build_image_override == expect

    @pytest.mark.parametrize(('config', 'fallback', 'expect'), [
        ("""\
          version: 1
          flatpak:
              base_image: fedora:latest
         """,
         "x", "fedora:latest"),
        ("""\
          version: 1
          flatpak: {}
         """,
         "x", "x"),
        ("""\
          version: 1
         """,
         "x", "x"),
        ("""\
          version: 1
         """,
         None, None),
        ("""\
          version: 1
          flatpak: {}
         """,
         None, None),
    ])
    def test_get_flatpak_base_image(self, config, fallback, expect):
        config_json = read_yaml(config, 'schemas/config.json')
        _, workflow = self.prepare()

        workflow.plugin_workspace[ReactorConfigPlugin.key] = {
            WORKSPACE_CONF_KEY: ReactorConfig(config_json)
        }

        kwargs = {}
        if fallback:
            kwargs['fallback'] = fallback

        if expect:
            base_image = get_flatpak_base_image(workflow, **kwargs)
            assert base_image == expect
        else:
            with pytest.raises(KeyError):
                get_flatpak_base_image(workflow, **kwargs)

    @pytest.mark.parametrize(('config', 'fallback', 'expect'), [
        ("""\
          version: 1
          flatpak:
              metadata: labels
         """,
         "annotations", "labels"),
        ("""\
          version: 1
          flatpak: {}
         """,
         "annotations", "annotations"),
        ("""\
          version: 1
         """,
         "annotations", "annotations"),
        ("""\
          version: 1
         """,
         None, None),
        ("""\
          version: 1
          flatpak: {}
         """,
         None, None),
    ])
    def test_get_flatpak_metadata(self, config, fallback, expect):
        config_json = read_yaml(config, 'schemas/config.json')
        _, workflow = self.prepare()

        workflow.plugin_workspace[ReactorConfigPlugin.key] = {
            WORKSPACE_CONF_KEY: ReactorConfig(config_json)
        }

        kwargs = {}
        if fallback:
            kwargs['fallback'] = fallback

        if expect:
            base_image = get_flatpak_metadata(workflow, **kwargs)
            assert base_image == expect
        else:
            with pytest.raises(KeyError):
                get_flatpak_metadata(workflow, **kwargs)

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
                  krb_principal: krb_principal
                  krb_keytab_path: /tmp/krb_keytab
              use_fast_upload: false
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
        _, workflow = self.prepare()
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

        use_fast_upload = config_json['koji'].get('use_fast_upload', True)

        fallback_map = {}
        if fallback:
            fallback_map = {'auth': deepcopy(auth_info), 'hub_url': config_json['koji']['hub_url']}
            fallback_map['auth']['krb_keytab_path'] = fallback_map['auth'].pop('krb_keytab')
            fallback_map['use_fast_upload'] = use_fast_upload
        else:
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] = \
                ReactorConfig(config_json)

        (flexmock(atomic_reactor.utils.koji)
            .should_receive('create_koji_session')
            .with_args(config_json['koji']['hub_url'], auth_info, use_fast_upload)
            .once()
            .and_return(True))

        get_koji_session(workflow, fallback_map)

    @pytest.mark.parametrize('fallback', (True, False))
    @pytest.mark.parametrize('root_url', (
        'https://koji.example.com/root',
        'https://koji.example.com/root/',
        None
    ))
    def test_get_koji_path_info(self, fallback, root_url):
        _, workflow = self.prepare()
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

        config = {
            'version': 1,
            'koji': {
                'hub_url': 'https://koji.example.com/hub',
                'auth': {
                    'ssl_certs_dir': '/var/certs'
                }
            }
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

        fallback_map = {}
        if fallback:
            fallback_map = deepcopy(config['koji'])
        else:
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] = \
                ReactorConfig(parsed_config)

        (flexmock(koji.PathInfo)
            .should_receive('__init__')
            .with_args(topdir=expected_root_url)
            .once())
        get_koji_path_info(workflow, fallback_map)

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
              timeout: 3600
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
    def test_get_odcs_session(self, tmpdir, config, raise_error):
        _, workflow = self.prepare()
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

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

        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig(config_json)

        if not ssl_dir_raise:
            (flexmock(atomic_reactor.utils.odcs.ODCSClient)
                .should_receive('__init__')
                .with_args(config_json['odcs']['api_url'], **auth_info)
                .once()
                .and_return(None))

            get_odcs_session(workflow)
        else:
            with pytest.raises(KeyError):
                get_odcs_session(workflow)

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
        _, workflow = self.prepare()
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

    @pytest.mark.parametrize(('config', 'error'), [
        ("""\
          version: 1
          cachito:
              api_url: https://cachito.example.com
              auth:
                  ssl_certs_dir: /var/run/secrets/atomic-reactor/cachitosecret
              timeout: 1000
        """, False),

        ("""\
          version: 1
          cachito:
              api_url: https://cachito.example.com
              insecure: true
              auth:
                  ssl_certs_dir: /var/run/secrets/atomic-reactor/cachitosecret
        """, False),

        ("""\
          version: 1
          cachito:
              api_url: https://cachito.example.com
              auth:
        """, OsbsValidationException),

        ("""\
          version: 1
          cachito:
              api_url: https://cachito.example.com
        """, OsbsValidationException),

        ("""\
          version: 1
          cachito:
              auth:
                  ssl_certs_dir: /var/run/secrets/atomic-reactor/cachitosecret
        """, OsbsValidationException),

        ("""\
          version: 1
          cachito:
              api_url: https://cachito.example.com
              auth:
                  ssl_certs_dir: /var/run/secrets/atomic-reactor/cachitosecret
              spam: ham
        """, OsbsValidationException),

        ("""\
          version: 1
          cachito:
              api_url: https://cachito.example.com
              auth:
                  ssl_certs_dir: nonexistent
        """, False),
    ])
    def test_get_cachito_session(self, tmpdir, config, error):
        _, workflow = self.prepare()
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

        if error:
            with pytest.raises(error):
                read_yaml(config, 'schemas/config.json')
            return
        config_json = read_yaml(config, 'schemas/config.json')

        auth_info = {
            'insecure': config_json['cachito'].get('insecure', False),
            'timeout': config_json['cachito'].get('timeout', False),
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

        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig(config_json)

        if not ssl_dir_raise:
            (flexmock(atomic_reactor.utils.cachito.CachitoAPI)
                .should_receive('__init__')
                .with_args(config_json['cachito']['api_url'], **auth_info)
                .once()
                .and_return(None))

            get_cachito_session(workflow)
        else:
            with pytest.raises(RuntimeError, match="Cachito ssl_certs_dir doesn't exist"):
                get_cachito_session(workflow)

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
        _, workflow = self.prepare()
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

    @pytest.mark.parametrize('config, valid', [
        ("""\
          version: 1
          operator_manifests:
              allowed_registries: null
        """, True),  # minimal valid example, allows all registries
        ("""\
          version: 1
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
          version: 1
          operator_manifests: null
        """, False),  # has to be a dict
        ("""\
          version: 1
          operator_manifests: {}
        """, False),  # allowed_registries is required
        ("""\
          version: 1
          operator_manifests:
              allowed_registries: []
        """, False),  # if not null, allowed_registries must not be empty
        ("""\
          version: 1
          operator_manifests:
              allowed_registries: null
              something_else: null
        """, False),  # additional properties not allowed
        ("""\
          version: 1
          operator_manifests:
              allowed_registries: null
              registry_post_replace:
                - old: foo
        """, False),  # missing replacement registry
        ("""\
          version: 1
          operator_manifests:
              allowed_registries: null
              registry_post_replace:
                - new: foo
        """, False),  # missing original registry
        ("""\
          version: 1
          operator_manifests:
              allowed_registries: null
              repo_replacements:
                - registry: foo
        """, False),  # missing package mappings url
        ("""\
          version: 1
          operator_manifests:
              allowed_registries: null
              repo_replacements:
                - package_mappings_url: https://somewhere.net/mapping.yaml
        """, False),  # missing registry
        ("""\
          version: 1
          operator_manifests:
              allowed_registries: null,
              repo_replacements:
                - registry: foo
                  package_mappings_url: mapping.yaml
        """, False),  # package mappings url is not a url
    ])
    def test_get_operator_manifests(self, config, valid):
        _, workflow = self.prepare()
        if valid:
            config_json = read_yaml(config, 'schemas/config.json')
        else:
            with pytest.raises(OsbsValidationException):
                read_yaml(config, 'schemas/config.json')
            return

        reactor_config_results = workflow.plugin_workspace.setdefault(ReactorConfigPlugin.key, {})
        reactor_config_results[WORKSPACE_CONF_KEY] = ReactorConfig(config_json)

        operator_config = get_operator_manifests(workflow)
        assert isinstance(operator_config, dict)
        assert "allowed_registries" in operator_config

    def test_set_user_params(self, tmpdir):
        filename = os.path.join(str(tmpdir), 'config.yaml')
        with open(filename, 'w'):
            pass

        tasker, workflow = self.prepare()
        plugin = ReactorConfigPlugin(tasker, workflow,
                                     config_path=str(tmpdir),
                                     basename=filename)
        plugin.run()
        assert plugin.workflow.user_params == USER_PARAMS

    @pytest.mark.parametrize('config, valid', [
        ("""\
          version: 1
          build_env_vars: []
        """, True),
        ("""\
          version: 1
          build_env_vars:
          - name: HTTP_PROXY
            value: example.proxy.net
          - name: NO_PROXY
            value: localhost
        """, True),
        ("""\
          version: 1
          build_env_vars:
          - name: FOO
            value: 1
        """, False),  # values must be strings
        ("""\
          version: 1
          build_env_vars:
          - name: FOO
        """, False),  # values must be defined
    ])
    def test_validate_build_env_vars(self, config, valid):
        # Only test schema validation, atomic-reactor has no additional support
        # for build_env_vars (osbs-client does, however)
        if valid:
            read_yaml(config, 'schemas/config.json')
        else:
            with pytest.raises(OsbsValidationException):
                read_yaml(config, 'schemas/config.json')


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
