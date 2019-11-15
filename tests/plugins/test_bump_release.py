"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import os
from copy import deepcopy
from textwrap import dedent

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
from atomic_reactor.plugins.pre_check_and_set_rebuild import CheckAndSetRebuildPlugin
from atomic_reactor.plugins.pre_fetch_sources import PLUGIN_FETCH_SOURCES_KEY
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor.util import df_parser
from atomic_reactor.constants import PROG
from flexmock import flexmock
import time
import pytest


KOJI_SOURCE_NVR = "sources_nvr"


class MockedClientSessionGeneral(object):
    def __init__(self, hub, opts=None):
        pass

    def getBuild(self, build_info):
        return None

    def krb_login(self, *args, **kwargs):
        return True


class MockSource(object):
    def __init__(self, tmpdir, add_timestamp=None):
        self.dockerfile_path = str(tmpdir.join('Dockerfile'))
        self.path = str(tmpdir)
        self.commit_id = None
        if add_timestamp is not None:
            self.config = flexmock(autorebuild=dict(add_timestamp_to_release=add_timestamp))
        else:
            self.config = flexmock(autorebuild=dict())


class TestBumpRelease(object):
    def prepare(self,
                tmpdir,
                labels=None,
                include_target=True,
                certs=False,
                append=False,
                reactor_config_map=False,
                reserve_build=False,
                is_auto=False,
                add_timestamp=None,
                fetch_source=False):
        if labels is None:
            labels = {}

        workflow = flexmock()
        setattr(workflow, 'builder', flexmock())
        setattr(workflow, 'plugin_workspace', {})
        setattr(workflow, 'reserved_build_id', None)
        setattr(workflow, 'reserved_token ', None)
        setattr(workflow, 'source', MockSource(tmpdir, add_timestamp))
        setattr(workflow, 'prebuild_results', {CheckAndSetRebuildPlugin.key: is_auto})
        if fetch_source:
            workflow.prebuild_results[PLUGIN_FETCH_SOURCES_KEY] = {
                'sources_for_nvr': KOJI_SOURCE_NVR
            }

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
            'auth': {},
            'reserve_build': reserve_build
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

    @pytest.mark.parametrize('add_timestamp', [True, False])
    @pytest.mark.parametrize('is_auto', [True, False])
    @pytest.mark.parametrize('scratch', [True, False])
    @pytest.mark.parametrize('build_exists', [True, False])
    @pytest.mark.parametrize('release_label', [
         'release',
         'Release',
    ])
    def test_release_label_already_set(self, tmpdir, caplog, add_timestamp, is_auto, scratch,
                                       build_exists, release_label, reactor_config_map):
        class MockedClientSession(object):
            def __init__(self, hub, opts=None):
                pass

            def getBuild(self, build_info):
                if build_exists:
                    return {'id': 12345}
                return build_exists

            def krb_login(self, *args, **kwargs):
                return True

        session = MockedClientSession('')
        flexmock(koji, ClientSession=session)

        new_environ = deepcopy(os.environ)
        new_environ["BUILD"] = dedent('''\
            {
              "metadata": {
              "labels": {}
              }
            }
            ''')
        if scratch:
            new_environ["BUILD"] = dedent('''\
                {
                  "metadata": {
                    "labels": {"scratch": "true"}
                  }
                }
                ''')
        flexmock(os)
        os.should_receive("environ").and_return(new_environ)  # pylint: disable=no-member

        plugin = self.prepare(tmpdir, labels={release_label: '1',
                                              'com.redhat.component': 'component',
                                              'version': 'version'},
                              add_timestamp=add_timestamp, is_auto=is_auto,
                              reactor_config_map=reactor_config_map)

        if build_exists and not scratch:
            with pytest.raises(RuntimeError) as exc:
                plugin.run()
            assert 'build already exists in Koji: ' in str(exc.value)
        else:
            plugin.run()

        timestamp_msg = 'autorebuild with add_timestamp_to_release and release ' \
                        'set explicitly, appending timestamp:'

        if is_auto and add_timestamp:
            assert timestamp_msg in caplog.text
        else:
            assert 'not incrementing' in caplog.text

    @pytest.mark.parametrize(('labels', 'all_wrong_labels'), [
        ({'com.redhat.component': 'component'},
         {'version': 'missing'}),

        ({'BZComponent': 'component'},
         {'version': 'missing'}),

        ({'version': 'version'},
         {'com.redhat.component': 'missing'}),

        ({'Version': 'version'},
         {'com.redhat.component': 'missing'}),

        ({},
         {'com.redhat.component': 'missing', 'version': 'missing'}),

        ({'com.redhat.component': 'component', 'version': ''},
         {'version': 'empty'}),

        ({'com.redhat.component': 'component', 'version': '$UNDEFINED'},
         {'version': 'empty'}),

        ({'com.redhat.component': 'component', 'version': 'version', 'release': ''},
         {'release': 'empty'}),

        ({'com.redhat.component': 'component', 'version': 'version', 'release': '$UNDEFINED'},
         {'release': 'empty'}),
    ])
    def test_missing_labels(self, tmpdir, caplog, reactor_config_map, labels, all_wrong_labels):
        session = MockedClientSessionGeneral('')
        flexmock(koji, ClientSession=session)
        plugin = self.prepare(tmpdir, labels=labels, reactor_config_map=reactor_config_map)
        with pytest.raises(RuntimeError) as exc:
            plugin.run()

        for label, status in all_wrong_labels.items():
            msg = '{} label: {}'.format(status, label)
            assert msg in caplog.text

        msg = 'Required labels are missing or empty or using' \
              ' undefined variables: {}'.format(all_wrong_labels)
        assert msg in str(exc.value)

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
    @pytest.mark.parametrize('reserve_build, init_fails', [
        (True, RuntimeError),
        (True, koji.GenericError),
        (True, None),
        (False, None)
    ])
    @pytest.mark.parametrize('next_release, base_release, append', [
        ({'actual': '1', 'builds': [], 'expected': '1', 'scratch': False},
         None, False),
        ({'actual': '1', 'builds': ['1'], 'expected': '2', 'scratch': False},
         None, False),
        ({'actual': '1', 'builds': ['1', '2'], 'expected': '3', 'scratch': False},
         None, False),
        ({'actual': '20', 'builds': ['19.1'], 'expected': '20', 'scratch': False},
         None, False),
        ({'actual': '20', 'builds': ['20', '20.1'], 'expected': '21', 'scratch': False},
         None, False),
        ({'actual': '20.1', 'builds': ['19.1'], 'expected': '20', 'scratch': False},
         None, False),
        ({'actual': '20.1', 'builds': ['19.1', '20'], 'expected': '21', 'scratch': False},
         None, False),
        ({'actual': '20.1', 'builds': ['20'], 'expected': '21', 'scratch': False},
         None, False),
        ({'actual': '20.1', 'builds': ['20', '20.1'], 'expected': '21', 'scratch': False},
         None, False),
        ({'actual': '20.2', 'builds': ['20', '20.1'], 'expected': '21', 'scratch': False},
         None, False),
        ({'actual': '20.2', 'builds': ['20', '20.1', '20.2'], 'expected': '21', 'scratch': False},
         None, False),
        ({'actual': '20.fc25', 'builds': ['20.fc24'], 'expected': '20.fc25', 'scratch': False},
         None, False),
        ({'actual': '20.fc25', 'builds': ['20.fc25'], 'expected': '21.fc25', 'scratch': False},
         None, False),
        ({'actual': '20.foo.fc25', 'builds': ['20.foo.fc25'],
         'expected': '21.foo.fc25', 'scratch': False},
         None, False),
        ({'actual': '20.1.fc25', 'builds': ['20.fc25', '20.1.fc25'],
         'expected': '21.fc25', 'scratch': False},
         None, False),
        ({'actual': '20.1.fc25', 'builds': ['20.fc25', '20.1.fc25', '21.fc25'],
         'expected': '22.fc25', 'scratch': False},
         None, False),
        ({'build_name': False, 'expected': '1', 'scratch': True},
         None, False),
        ({'build_name': False, 'expected': '1', 'scratch': True},
         None, True),
        ({'build_name': True, 'expected': 'scratch-123456', 'scratch': True},
         None, False),
        ({'build_name': True, 'expected': 'scratch-123456', 'scratch': True},
         None, True),
        ({'builds': [], 'expected': '42.1', 'scratch': False},
         '42', True),
        ({'builds': ['42.1', '42.2'], 'expected': '42.3', 'scratch': False},
         '42', True),
        # No interpretation of the base release when appending - just treated as string
        ({'builds': ['42.2'], 'expected': '42.1.1', 'scratch': False},
         '42.1', True),
        # No interpretation of the base release when appending - just treated as string
        ({'builds': ['42.1.1'], 'expected': '42.1.2', 'scratch': False},
         '42.1', True),
        ({'builds': [], 'expected': '1.1', 'scratch': False},
         None, True),
        ({'builds': ['1.1'], 'expected': '1.2', 'scratch': False},
         None, True),
        ({'builds': ['1.1', '1.2'], 'expected': '1.3', 'scratch': False},
         None, True),
    ])
    def test_increment_and_append(self, tmpdir, component, version, next_release, base_release,
                                  append, include_target, reserve_build, init_fails,
                                  reactor_config_map):
        build_id = '123456'
        token = 'token_123456'
        class MockedClientSession(object):
            def __init__(self, hub, opts=None):
                self.ca_path = None
                self.cert_path = None
                self.serverca_path = None

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

            def CGInitBuild(self, cg_name, nvr_data):
                assert cg_name == PROG
                assert nvr_data['name'] == list(component.values())[0]
                assert nvr_data['version'] == list(version.values())[0]
                assert nvr_data['release'] == next_release['expected']
                if init_fails:
                    raise init_fails('unable to pre-declare build {}'.format(nvr_data))
                return {'build_id': build_id, 'token': token}

        session = MockedClientSession('')
        flexmock(time).should_receive('sleep').and_return(None)
        flexmock(koji, ClientSession=session)

        labels = {}
        labels.update(component)
        labels.update(version)
        if base_release:
            labels['release'] = base_release

        plugin = self.prepare(tmpdir, labels=labels,
                              include_target=include_target,
                              certs=True,
                              reactor_config_map=reactor_config_map,
                              reserve_build=reserve_build,
                              append=append)

        new_environ = deepcopy(os.environ)
        new_environ["BUILD"] = dedent('''\
            {
              "metadata": {
              "labels": {}
              }
            }
            ''')
        if next_release['scratch']:
            new_environ = deepcopy(os.environ)
            new_environ["BUILD"] = dedent('''\
                {
                  "metadata": {
                    "labels": {"scratch": "true"}
                  }
                }
                ''')
            if next_release['build_name']:
                new_environ["BUILD"] = dedent('''\
                    {
                      "metadata": {
                        "name": "scratch-123456",
                        "labels": {"scratch": "true"}
                      }
                    }
                    ''')
        flexmock(os)
        os.should_receive("environ").and_return(new_environ)  # pylint: disable=no-member

        if init_fails and reserve_build and reactor_config_map and not next_release['scratch']:
            with pytest.raises(RuntimeError) as exc:
                plugin.run()
            assert 'unable to pre-declare build ' in str(exc.value)
            return

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

        if reserve_build and reactor_config_map and not next_release['scratch']:
            assert plugin.workflow.reserved_build_id == build_id
            assert plugin.workflow.reserved_token == token

    @pytest.mark.parametrize('reserve_build, init_fails', [
        (True, RuntimeError),
        (True, koji.GenericError),
        (True, None),
        (False, None)
    ])
    @pytest.mark.parametrize('next_release', [
        {'builds': [], 'scratch': False, 'expected': '1.1'},
        {'builds': ['1.1', '1.2'], 'scratch': False, 'expected': '1.3'},
        {'builds': [], 'scratch': True, 'expected': '1.scratch'},
        {'builds': ['1.1', '1.2'], 'scratch': True, 'expected': '1.scratch'},
    ])
    def test_source_build_release(self, tmpdir, next_release, reserve_build, init_fails,
                                  reactor_config_map):
        build_id = '123456'
        token = 'token_123456'
        koji_name = 'component'
        koji_version = '3.0'
        koji_release = '1'
        koji_source = 'git_reg/repo'

        class MockedClientSession(object):
            def __init__(self, hub, opts=None):
                self.ca_path = None
                self.cert_path = None
                self.serverca_path = None

            def getBuild(self, build_info):
                if isinstance(build_info, dict):
                    assert build_info['name'] == "%s-source" % koji_name
                    assert build_info['version'] == koji_version

                    if build_info['release'] in next_release['builds']:
                        return True
                    return None
                else:
                    return {'name': koji_name, 'version': koji_version,
                            'release': koji_release, 'source': koji_source}

            def ssl_login(self, cert=None, ca=None, serverca=None, proxyuser=None):
                self.ca_path = ca
                self.cert_path = cert
                self.serverca_path = serverca
                return True

            def krb_login(self, *args, **kwargs):
                return True

            def CGInitBuild(self, cg_name, nvr_data):
                assert cg_name == PROG
                assert nvr_data['name'] == "%s-source" % koji_name
                assert nvr_data['version'] == koji_version
                if init_fails:
                    raise init_fails('unable to pre-declare build {}'.format(nvr_data))
                return {'build_id': build_id, 'token': token}

        session = MockedClientSession('')
        flexmock(time).should_receive('sleep').and_return(None)
        flexmock(koji, ClientSession=session)

        plugin = self.prepare(tmpdir, certs=True, reactor_config_map=reactor_config_map,
                              reserve_build=reserve_build, fetch_source=True)

        new_environ = deepcopy(os.environ)
        if next_release['scratch']:
            new_environ = deepcopy(os.environ)
            new_environ["BUILD"] = dedent('''\
                {
                  "metadata": {
                    "labels": {"scratch": "true"}
                  }
                }
                ''')
        else:
            new_environ["BUILD"] = dedent('''\
                {
                  "metadata": {
                  "labels": {}
                  }
                }
                ''')
        flexmock(os)
        os.should_receive("environ").and_return(new_environ)  # pylint: disable=no-member

        if init_fails and reserve_build and reactor_config_map and not next_release['scratch']:
            with pytest.raises(RuntimeError) as exc:
                plugin.run()
            assert 'unable to pre-declare build ' in str(exc.value)
            return

        plugin.run()

        for file_path, expected in [(session.cert_path, 'cert'),
                                    (session.serverca_path, 'serverca')]:

            assert os.path.isfile(file_path)
            with open(file_path, 'r') as fd:
                assert fd.read() == expected

        if reserve_build and reactor_config_map and not next_release['scratch']:
            assert plugin.workflow.reserved_build_id == build_id
            assert plugin.workflow.reserved_token == token

        assert plugin.workflow.koji_source_source_url == koji_source

        expected_nvr = {'name': "%s-source" % koji_name,
                        'version': koji_version,
                        'release': next_release['expected']}
        plugin.workflow.koji_source_nvr = expected_nvr
