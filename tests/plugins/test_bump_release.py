"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import os

try:
    import koji as koji
except ImportError:
    import inspect
    import sys

    # Find our mocked koji module
    import tests.koji as koji
    mock_koji_path = os.path.dirname(inspect.getfile(koji.ClientSession))
    if mock_koji_path not in sys.path:
        sys.path.append(os.path.dirname(mock_koji_path))

    # Now load it properly, the same way the plugin will
    del koji
    import koji as koji

from atomic_reactor.plugins.pre_bump_release import BumpReleasePlugin
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor.util import df_parser
from flexmock import flexmock
import pytest


class MockedClientSessionGeneral(object):
    def __init__(self, hub, opts=None):
        pass

    def getBuild(self, build_info):
        return None

    def krb_login(self, *args, **kwargs):
        return True


class TestBumpRelease(object):
    def prepare(self,
                tmpdir,
                labels=None,
                include_target=True,
                certs=False,
                append=False,
                reactor_config_map=False):
        if labels is None:
            labels = {}

        workflow = flexmock()
        setattr(workflow, 'builder', flexmock())
        setattr(workflow, 'plugin_workspace', {})

        df = tmpdir.join('Dockerfile')
        df.write('FROM base\n')
        for key, value in labels.items():
            df.write('LABEL {key}={value}\n'.format(key=key, value=value), mode='a')
        setattr(workflow.builder, 'df_path', str(df))

        kwargs = {
            'tasker': None,
            'workflow': workflow,
            'hub': ''
        }
        koji_map = {
            'hub_url': '',
            'root_url': '',
            'auth': {}
        }
        if include_target:
            kwargs['target'] = 'foo'
        if append:
            kwargs['append'] = True
        if certs:
            tmpdir.join('cert').write('cert')
            tmpdir.join('serverca').write('serverca')
            kwargs['koji_ssl_certs_dir'] = str(tmpdir)
            koji_map['auth']['ssl_certs_dir'] = str(tmpdir)

        if reactor_config_map:
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1, 'koji': koji_map})

        plugin = BumpReleasePlugin(**kwargs)
        return plugin

    def test_component_missing(self, tmpdir, reactor_config_map):  # noqa
        session = MockedClientSessionGeneral('')
        flexmock(koji, ClientSession=session)
        plugin = self.prepare(tmpdir, reactor_config_map=reactor_config_map)
        with pytest.raises(RuntimeError):
            plugin.run()

    @pytest.mark.parametrize('release_label', [
         'release',
         'Release',
    ])
    def test_release_label_already_set(self, tmpdir, caplog, release_label,
                                       reactor_config_map):
        session = MockedClientSessionGeneral('')
        flexmock(koji, ClientSession=session)
        plugin = self.prepare(tmpdir, labels={release_label: '1'},
                              reactor_config_map=reactor_config_map)
        plugin.run()
        assert 'not incrementing' in caplog.text()

    @pytest.mark.parametrize('labels', [
        {'com.redhat.component': 'component'},
        {'BZComponent': 'component'},
        {'version': 'version'},
        {'Version': 'version'},
        {},
    ])
    def test_missing_labels(self, tmpdir, caplog, labels, reactor_config_map):
        session = MockedClientSessionGeneral('')
        flexmock(koji, ClientSession=session)
        plugin = self.prepare(tmpdir, labels=labels, reactor_config_map=reactor_config_map)
        with pytest.raises(RuntimeError) as exc:
            plugin.run()
        assert 'missing label' in str(exc)

    @pytest.mark.parametrize('component', [
        {'com.redhat.component': 'component1'},
        {'BZComponent': 'component2'},
    ])
    @pytest.mark.parametrize('version', [
        {'version': '7.1'},
        {'Version': '7.2'},
    ])
    @pytest.mark.parametrize('include_target', [
        True,
        False
    ])
    @pytest.mark.parametrize('next_release', [
        {'actual': '1', 'builds': [], 'expected': '1'},
        {'actual': '1', 'builds': ['1'], 'expected': '2'},
        {'actual': '1', 'builds': ['1', '2'], 'expected': '3'},
        {'actual': '20', 'builds': ['19.1'], 'expected': '20'},
        {'actual': '20', 'builds': ['20', '20.1'], 'expected': '21'},
        {'actual': '20.1', 'builds': ['19.1'], 'expected': '20'},
        {'actual': '20.1', 'builds': ['19.1', '20'], 'expected': '21'},
        {'actual': '20.1', 'builds': ['20'], 'expected': '21'},
        {'actual': '20.1', 'builds': ['20', '20.1'], 'expected': '21'},
        {'actual': '20.2', 'builds': ['20', '20.1'], 'expected': '21'},
        {'actual': '20.2', 'builds': ['20', '20.1', '20.2'], 'expected': '21'},
        {'actual': '20.fc25', 'builds': ['20.fc24'], 'expected': '20.fc25'},
        {'actual': '20.fc25', 'builds': ['20.fc25'], 'expected': '21.fc25'},
        {'actual': '20.foo.fc25',
         'builds': ['20.foo.fc25'],
         'expected': '21.foo.fc25'},
        {'actual': '20.1.fc25',
         'builds': ['20.fc25', '20.1.fc25'],
         'expected': '21.fc25'},
        {'actual': '20.1.fc25',
         'builds': ['20.fc25', '20.1.fc25', '21.fc25'],
         'expected': '22.fc25'},
    ])
    def test_increment(self, tmpdir, component, version, next_release,
                       include_target, reactor_config_map):

        class MockedClientSession(object):
            def __init__(self, hub, opts=None):
                pass

            def getNextRelease(self, build_info):
                assert build_info['name'] == list(component.values())[0]
                assert build_info['version'] == list(version.values())[0]
                return next_release['actual']

            def getBuild(self, build_info):
                assert build_info['name'] == list(component.values())[0]
                assert build_info['version'] == list(version.values())[0]

                if build_info['release'] in next_release['builds']:
                    return True
                return None

            def ssl_login(self, cert=None, ca=None, serverca=None, proxyuser=None):
                self.ca_path = ca
                self.cert_path = cert
                self.serverca_path = serverca
                return True

            def krb_login(self, *args, **kwargs):
                return True

        session = MockedClientSession('')
        flexmock(koji, ClientSession=session)

        labels = {}
        labels.update(component)
        labels.update(version)

        plugin = self.prepare(tmpdir, labels=labels,
                              include_target=include_target,
                              certs=True,
                              reactor_config_map=reactor_config_map)
        plugin.run()

        for file_path, expected in [(session.cert_path, 'cert'),
                                    (session.serverca_path, 'serverca')]:

            assert os.path.isfile(file_path)
            with open(file_path, 'r') as fd:
                assert fd.read() == expected

        parser = df_parser(plugin.workflow.builder.df_path, workflow=plugin.workflow)
        assert parser.labels['release'] == next_release['expected']
        # Old-style spellings should not be asserted
        assert 'Release' not in parser.labels

    @pytest.mark.parametrize('base_release,builds,expected', [
        ('42', [], '42.1'),
        ('42', ['42.1', '42.2'], '42.3'),
        # No interpretation of the base release when appending - just treated as string
        ('42.1', ['42.2'], '42.1.1'),
        ('42.1', ['42.1.1'], '42.1.2'),
        (None, [], '1.1'),
        (None, ['1.1'], '1.2'),
        (None, ['1.1', '1.2'], '1.3'),
    ])
    def test_append(self, tmpdir, base_release, builds, expected, reactor_config_map):

        class MockedClientSession(object):
            def __init__(self, hub, opts=None):
                pass

            def getBuild(self, build_info):
                if build_info['release'] in builds:
                    return True
                return None

            def krb_login(self, *args, **kwargs):
                return True

        session = MockedClientSession('')
        flexmock(koji, ClientSession=session)

        labels = {
            'com.redhat.component': 'component1',
            'version': 'fc26',
        }
        if base_release:
            labels['release'] = base_release

        plugin = self.prepare(tmpdir, labels=labels,
                              append=True, reactor_config_map=reactor_config_map)
        plugin.run()

        parser = df_parser(plugin.workflow.builder.df_path, workflow=plugin.workflow)
        assert parser.labels['release'] == expected
