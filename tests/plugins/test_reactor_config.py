"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from jsonschema import ValidationError
import io
import logging
import os
import pkg_resources
import pytest
from textwrap import dedent
import re
import yaml

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfig,
                                                       ReactorConfigPlugin,
                                                       get_config)
from tests.constants import TEST_IMAGE
from tests.docker_mock import mock_docker
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
        assert plugin.run() == None

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
    def test_bad_cluster_config(self, tmpdir, caplog, config, errors):
        filename = os.path.join(str(tmpdir), 'config.yaml')
        with open(filename, 'w') as fp:
            fp.write(dedent(config))
        tasker, workflow = self.prepare()
        plugin = ReactorConfigPlugin(tasker, workflow, config_path=str(tmpdir))

        with caplog.atLevel(logging.ERROR), pytest.raises(ValidationError):
            plugin.run()

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
    def test_good_cluster_config(self, tmpdir, config, clusters):
        filename = os.path.join(str(tmpdir), 'config.yaml')
        with open(filename, 'w') as fp:
            fp.write(dedent(config))
        tasker, workflow = self.prepare()
        plugin = ReactorConfigPlugin(tasker, workflow, config_path=str(tmpdir))
        assert plugin.run() == None

        conf = get_config(workflow)
        enabled = conf.get_enabled_clusters_for_platform('platform')
        assert set([(x.name, x.max_concurrent_builds)
                    for x in enabled]) == set(clusters)
