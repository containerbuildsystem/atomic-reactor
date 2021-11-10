"""
Copyright (c) 2015, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import koji
import os

from atomic_reactor.plugins.pre_bump_release import BumpReleasePlugin
from atomic_reactor.plugins.pre_fetch_sources import PLUGIN_FETCH_SOURCES_KEY
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.util import df_parser
from atomic_reactor.constants import PROG
from tests.util import add_koji_map_in_workflow
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
                certs=False,
                append=False,
                reserve_build=False,
                add_timestamp=None,
                fetch_source=False,
                scratch=None):
        if labels is None:
            labels = {}

        workflow = DockerBuildWorkflow(source=None)
        workflow.source = MockSource(tmpdir, add_timestamp)
        if scratch is not None:
            workflow.user_params['scratch'] = scratch
        if fetch_source:
            workflow.prebuild_results[PLUGIN_FETCH_SOURCES_KEY] = {
                'sources_for_nvr': KOJI_SOURCE_NVR
            }

        df = tmpdir.join('Dockerfile')
        df.write('FROM base\n')
        for key, value in labels.items():
            df.write('LABEL {key}={value}\n'.format(key=key, value=value), mode='a')
        flexmock(workflow, df_path=str(df))

        kwargs = {
            'workflow': workflow,
        }

        if append:
            kwargs['append'] = True
        if certs:
            tmpdir.join('cert').write('cert')
            tmpdir.join('serverca').write('serverca')

        add_koji_map_in_workflow(workflow, hub_url='', root_url='',
                                 reserve_build=reserve_build,
                                 ssl_certs_dir=str(tmpdir) if certs else None)

        plugin = BumpReleasePlugin(**kwargs)
        return plugin

    def test_component_missing(self, tmpdir, user_params):
        session = MockedClientSessionGeneral('')
        flexmock(koji, ClientSession=session)
        plugin = self.prepare(tmpdir)
        with pytest.raises(RuntimeError):
            plugin.run()

    @pytest.mark.parametrize(('reserve_build', 'koji_build_status', 'init_fails'), [
        (True, 'COMPLETE', True),
        (True, 'FAILED', True),
        (True, 'CANCELED', True),
        (True, 'COMPLETE', False),
        (True, 'FAILED', False),
        (True, 'CANCELED', False),
        (False, 'COMPLETE', False),
        (False, 'FAILED', False),
        (False, 'CANCELED', False),
    ])
    @pytest.mark.parametrize('scratch', [True, False])
    @pytest.mark.parametrize('build_exists', [True, False])
    @pytest.mark.parametrize('release_label', [
         'release',
         'Release',
    ])
    @pytest.mark.parametrize('user_provided_relese', [True, False])
    def test_release_label_already_set(self, tmpdir, caplog, reserve_build, koji_build_status,
                                       init_fails, scratch,
                                       build_exists, release_label, user_provided_relese,
                                       user_params):
        class MockedClientSession(object):
            def __init__(self, hub, opts=None):
                pass

            def getBuild(self, build_info):
                if build_exists:
                    return {'id': 12345, 'state': koji.BUILD_STATES[koji_build_status]}
                return build_exists

            def krb_login(self, *args, **kwargs):
                return True

            def CGInitBuild(self, cg_name, nvr_data):
                if init_fails:
                    raise koji.GenericError('unable to pre-declare build {}'.format(nvr_data))

                return {'build_id': 'reserved_build', 'token': 'reserved_token'}

        session = MockedClientSession('')
        flexmock(koji, ClientSession=session)

        plugin = self.prepare(tmpdir, labels={release_label: '1',
                                              'com.redhat.component': 'component',
                                              'version': 'version'},
                              reserve_build=reserve_build,
                              scratch=scratch)

        if user_provided_relese:
            plugin.workflow.user_params['release'] = 'release_provided'

        refund_build = (reserve_build and koji_build_status != 'COMPLETE')
        if build_exists and not scratch and not refund_build:
            with pytest.raises(RuntimeError) as exc:
                plugin.run()
            assert 'build already exists in Koji: ' in str(exc.value)
            return

        if reserve_build and init_fails and not scratch:
            with pytest.raises(RuntimeError) as exc:
                plugin.run()

            assert 'unable to pre-declare build ' in str(exc.value)
            return

        plugin.run()

        if not user_provided_relese:
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
    def test_missing_labels(self, tmpdir, caplog, labels, all_wrong_labels, user_params):
        session = MockedClientSessionGeneral('')
        flexmock(koji, ClientSession=session)
        plugin = self.prepare(tmpdir, labels=labels)
        with pytest.raises(RuntimeError) as exc:
            plugin.run()

        for label, status in all_wrong_labels.items():
            msg = '{} label: {}'.format(status, label)
            assert msg in caplog.text

        msg = 'Required labels are missing or empty or using' \
              ' undefined variables: {}'.format(all_wrong_labels)
        assert msg in str(exc.value)

    @pytest.mark.parametrize(('component', 'version'), [
        ({'com.redhat.component': 'component1'}, {'version': '7.1'}),
        ({'com.redhat.component': 'component1'}, {'Version': '7.2'}),
        ({'BZComponent': 'component2'}, {'version': '7.1'}),
        ({'BZComponent': 'component2'}, {'Version': '7.2'}),
    ])
    @pytest.mark.parametrize('reserve_build, init_fails, koji_build_state', [
        (True, RuntimeError, 'COMPLETE'),
        (True, koji.GenericError, 'COMPLETE'),
        (True, None, 'COMPLETE'),
        (True, None, 'FAILED'),
        (True, None, 'CANCELED'),
        (False, None, 'COMPLETE')
    ])
    @pytest.mark.parametrize('next_release, base_release, append', [
        ({'actual': '1', 'builds': [],
          'expected': '1', 'expected_refund': '1', 'scratch': False},
         None, False),

        ({'actual': '1', 'builds': ['1'],
          'expected': '2', 'expected_refund': '1', 'scratch': False},
         None, False),

        ({'actual': '1', 'builds': ['1', '2'],
          'expected': '3', 'expected_refund': '1', 'scratch': False},
         None, False),

        ({'actual': '20', 'builds': ['19.1'],
          'expected': '20', 'expected_refund': '20', 'scratch': False},
         None, False),

        ({'actual': '20', 'builds': ['20', '20.1'],
          'expected': '21', 'expected_refund': '20', 'scratch': False},
         None, False),

        ({'actual': '20.1', 'builds': ['19.1'],
          'expected': '20', 'expected_refund': '20', 'scratch': False},
         None, False),

        ({'actual': '20.1', 'builds': ['19.1', '20'],
          'expected': '21', 'expected_refund': '20', 'scratch': False},
         None, False),

        ({'actual': '20.1', 'builds': ['20'],
          'expected': '21', 'expected_refund': '20', 'scratch': False},
         None, False),

        ({'actual': '20.1', 'builds': ['20', '20.1'],
          'expected': '21', 'expected_refund': '20', 'scratch': False},
         None, False),

        ({'actual': '20.2', 'builds': ['20', '20.1'],
          'expected': '21', 'expected_refund': '20', 'scratch': False},
         None, False),

        ({'actual': '20.2', 'builds': ['20', '20.1', '20.2'],
          'expected': '21', 'expected_refund': '20', 'scratch': False},
         None, False),

        ({'actual': '20.fc25', 'builds': ['20.fc24'],
          'expected': '20.fc25', 'expected_refund': '20.fc25', 'scratch': False},
         None, False),

        ({'actual': '20.fc25', 'builds': ['20.fc25'],
          'expected': '21.fc25', 'expected_refund': '20.fc25', 'scratch': False},
         None, False),

        ({'actual': '20.foo.fc25', 'builds': ['20.foo.fc25'],
         'expected': '21.foo.fc25', 'expected_refund': '20.foo.fc25', 'scratch': False},
         None, False),

        ({'actual': '20.1.fc25', 'builds': ['20.fc25', '20.1.fc25'],
         'expected': '21.fc25', 'expected_refund': '20.fc25', 'scratch': False},
         None, False),

        ({'actual': '20.1.fc25', 'builds': ['20.fc25', '20.1.fc25', '21.fc25'],
         'expected': '22.fc25', 'expected_refund': '20.fc25', 'scratch': False},
         None, False),

        ({'build_name': True, 'expected': 'scratch-123456', 'expected_refund': 'scratch-123456',
          'scratch': True},
         None, False),

        ({'build_name': True, 'expected': 'scratch-123456', 'expected_refund': 'scratch-123456',
          'scratch': True},
         None, True),

        ({'builds': [], 'expected': '42.1', 'expected_refund': '42.1', 'scratch': False},
         '42', True),

        ({'builds': ['42.1', '42.2'], 'expected': '42.3', 'expected_refund': '42.1',
          'scratch': False},
         '42', True),

        # No interpretation of the base release when appending - just treated as string
        ({'builds': ['42.2'], 'expected': '42.1.1', 'expected_refund': '42.1.1', 'scratch': False},
         '42.1', True),

        # No interpretation of the base release when appending - just treated as string
        ({'builds': ['42.1.1'], 'expected': '42.1.2', 'expected_refund': '42.1.1',
          'scratch': False},
         '42.1', True),

        ({'builds': [], 'expected': '1.1', 'expected_refund': '1.1', 'scratch': False},
         None, True),

        ({'builds': ['1.1'], 'expected': '1.2', 'expected_refund': '1.1', 'scratch': False},
         None, True),

        ({'builds': ['1.1', '1.2'], 'expected': '1.3', 'expected_refund': '1.1', 'scratch': False},
         None, True),
    ])
    def test_increment_and_append(self, tmpdir, component, version, next_release, base_release,
                                  append, reserve_build, init_fails, koji_build_state, user_params):
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
                    return {'state': koji.BUILD_STATES[koji_build_state]}
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
                if reserve_build and koji_build_state != 'COMPLETE':
                    assert nvr_data['release'] == next_release['expected_refund']
                else:
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
                              certs=True, reserve_build=reserve_build,
                              append=append, scratch=next_release['scratch'])

        if next_release['scratch'] and next_release['build_name']:
            plugin.workflow.user_params['pipeline_run_name'] = "scratch-123456"

        if init_fails and reserve_build and not next_release['scratch']:
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

        parser = df_parser(plugin.workflow.df_path, workflow=plugin.workflow)
        if reserve_build and koji_build_state != 'COMPLETE':
            assert parser.labels['release'] == next_release['expected_refund']
        else:
            assert parser.labels['release'] == next_release['expected']
        # Old-style spellings should not be asserted
        assert 'Release' not in parser.labels

        if reserve_build and not next_release['scratch']:
            assert plugin.workflow.reserved_build_id == build_id
            assert plugin.workflow.reserved_token == token

    @pytest.mark.parametrize('next_release', [
        {'last': '20', 'builds': ['19', '20'], 'expected': '21', 'search': [{'id': 12345}]},

        {'last': '19', 'builds': ['19', '20'], 'expected': '21', 'search': [{'id': 12345}]},

        {'last': '19.1', 'builds': ['19.1'], 'expected': '20', 'search': [{'id': 12345}]},

        {'builds': ['1', '2'], 'expected': '3', 'search': []},
    ])
    def test_get_next_release(self, tmpdir, next_release, user_params):
        build_id = '123456'
        token = 'token_123456'
        component = {'com.redhat.component': 'component1'}
        version = {'version': '7.1'}
        koji_build_state = 'COMPLETE'

        class MockedClientSession(object):
            def __init__(self, hub, opts=None):
                self.ca_path = None
                self.cert_path = None
                self.serverca_path = None

            def getNextRelease(self, build_info):
                raise koji.BuildError('Unable to increment release')

            def search(self, terms, searchtype, matchType, queryOpts=None):
                return next_release['search']

            def getBuild(self, build_info):
                if isinstance(build_info, int):
                    return {'release': next_release['last']}

                assert build_info['name'] == list(component.values())[0]
                assert build_info['version'] == list(version.values())[0]

                if build_info['release'] in next_release['builds']:
                    return {'state': koji.BUILD_STATES[koji_build_state]}
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
                return {'build_id': build_id, 'token': token}

        session = MockedClientSession('')
        flexmock(time).should_receive('sleep').and_return(None)
        flexmock(koji, ClientSession=session)

        labels = {}
        labels.update(component)
        labels.update(version)

        plugin = self.prepare(tmpdir, labels=labels,
                              certs=True, reserve_build=True,
                              append=False)

        plugin.run()

        for file_path, expected in [(session.cert_path, 'cert'),
                                    (session.serverca_path, 'serverca')]:

            assert os.path.isfile(file_path)
            with open(file_path, 'r') as fd:
                assert fd.read() == expected

        parser = df_parser(plugin.workflow.df_path, workflow=plugin.workflow)
        assert parser.labels['release'] == next_release['expected']

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
                                  user_params):
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
                        return {'state': koji.BUILD_STATES['COMPLETE']}
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

        plugin = self.prepare(tmpdir, certs=True, reserve_build=reserve_build,
                              fetch_source=True, scratch=next_release['scratch'])

        if init_fails and reserve_build and not next_release['scratch']:
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

        if reserve_build and not next_release['scratch']:
            assert plugin.workflow.reserved_build_id == build_id
            assert plugin.workflow.reserved_token == token

        assert plugin.workflow.koji_source_source_url == koji_source

        expected_nvr = {'name': "%s-source" % koji_name,
                        'version': koji_version,
                        'release': next_release['expected']}
        plugin.workflow.koji_source_nvr = expected_nvr


@pytest.mark.parametrize('flatpak, isolated, append', [
    (True, True, False),
    (True, False, True),
    (False, True, False),
    (False, False, False)
])
def test_append_from_user_params(workflow, flatpak, isolated, append):
    workflow.user_params["flatpak"] = flatpak
    workflow.user_params["isolated"] = isolated
    add_koji_map_in_workflow(workflow, hub_url='', root_url='')

    session = MockedClientSessionGeneral('')
    flexmock(koji, ClientSession=session)

    runner = PreBuildPluginsRunner(workflow, [])
    plugin = runner.create_instance_from_plugin(BumpReleasePlugin, {})

    assert plugin.append == append
