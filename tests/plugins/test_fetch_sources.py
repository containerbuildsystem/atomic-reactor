"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import

import io
import os
import yaml
from textwrap import dedent
from copy import deepcopy

import koji
import pytest
import requests
from flexmock import flexmock
import time

from atomic_reactor import constants
from atomic_reactor import util
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_fetch_sources import FetchSourcesPlugin
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY, ReactorConfig)
import atomic_reactor
from tests.constants import TEST_IMAGE
from tests.stubs import StubInsideBuilder


KOJI_HUB = 'http://koji.com/hub'
KOJI_ROOT = 'http://koji.localhost/kojiroot'
KOJI_UPLOAD_TEST_WORKDIR = 'temp_workdir'
KOJI_BUILD = {'build_id': 1, 'nvr': 'foobar-1-1', 'name': 'foobar', 'version': 1, 'release': 1,
              'extra': {'image': {'parent_build_id': 10}, 'operator-manifests': {}},
              'source': 'registry.com/repo#ref'}
KOJI_PARENT_BUILD = {'build_id': 10, 'nvr': 'parent-1-1', 'name': 'parent', 'version': 1,
                     'release': 1,
                     'extra': {'image': {''}, 'operator-manifests': {}}}
constants.HTTP_BACKOFF_FACTOR = 0
REMOTE_SOURCES_FILE = 'remote-source.tar.gz'

DEFAULT_SIGNING_INTENT = 'empty'

BASE_CONFIG_MAP = dedent("""\
    version: 1
    koji:
       hub_url: {}
       root_url: {}
       auth:
           ssl_certs_dir: not_needed_here
    """.format(KOJI_HUB, KOJI_ROOT))


def mock_reactor_config(workflow, tmpdir, data=None, default_si=DEFAULT_SIGNING_INTENT):
    if data is None:
        data = dedent("""\
            version: 1
            koji:
               hub_url: {}
               root_url: {}
               auth:
                   ssl_certs_dir: not_needed_here
            odcs:
               signing_intents:
               - name: invalid
                 keys: ['notUsed']
               - name: one
                 keys: ['usedKey']
               - name: multiple
                 keys: ['notUsed', 'usedKey', 'notUsed2']
               - name: unsigned
                 keys: ['']
               - name: empty
                 keys: []
               default_signing_intent: {}
               api_url: invalid
               auth:
                   ssl_certs_dir: {}
            """.format(KOJI_HUB, KOJI_ROOT, default_si, tmpdir))

    workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

    config = {}
    if data:
        tmpdir.join('cert').write('')
        config = util.read_yaml(data, 'schemas/config.json')

    workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] = ReactorConfig(config)


def mock_workflow(tmpdir, for_orchestrator=False, config_map=None,
                  default_si=DEFAULT_SIGNING_INTENT):
    workflow = DockerBuildWorkflow(
        TEST_IMAGE
    )
    builder = StubInsideBuilder().for_workflow(workflow)
    builder.set_df_path(str(tmpdir))
    builder.tasker = flexmock()
    workflow.builder = flexmock(builder)

    if for_orchestrator:
        workflow.buildstep_plugins_conf = [{'name': constants.PLUGIN_BUILD_ORCHESTRATE_KEY}]

    mock_reactor_config(workflow, tmpdir, data=config_map, default_si=default_si)
    return workflow


def mock_env(tmpdir, docker_tasker, scratch=False, orchestrator=False, koji_build_id=None,
             koji_build_nvr=None, config_map=None, default_si=DEFAULT_SIGNING_INTENT):
    build_json = {'metadata': {'labels': {'scratch': scratch}}}
    flexmock(util).should_receive('get_build_json').and_return(build_json)
    workflow = mock_workflow(tmpdir, for_orchestrator=orchestrator, config_map=config_map,
                             default_si=default_si)
    plugin_conf = [{'name': FetchSourcesPlugin.key}]
    plugin_conf[0]['args'] = {
        'koji_build_id': koji_build_id,
        'koji_build_nvr': koji_build_nvr
    }

    flexmock(atomic_reactor.source.GitSource, get=str(tmpdir))
    runner = PreBuildPluginsRunner(docker_tasker, workflow, plugin_conf)
    return runner


@pytest.fixture()
def koji_session():
    session = flexmock()
    flexmock(session).should_receive('ssl_login').and_return(True)
    (flexmock(session)
     .should_receive('listArchives')
     .with_args(object, type='image')
     .and_return([{'id': 1}, {'id': 2}]))
    (flexmock(session)
     .should_receive('listArchives')
     .with_args(object, type='remote-sources')
     .and_return([{'id': 1, 'type_name': 'tar', 'filename': REMOTE_SOURCES_FILE}]))
    flexmock(session).should_receive('listRPMs').with_args(imageID=1).and_return([
        {'id': 1,
         'build_id': 1,
         'nvr': 'foobar-1-1',
         'arch': 'x86_64',
         'external_repo_name': 'INTERNAL'}
    ])
    flexmock(session).should_receive('listRPMs').with_args(imageID=2).and_return([
        {'id': 2,
         'build_id': 1,
         'nvr': 'foobar-1-1',
         'arch': 'x86_64',
         'external_repo_name': 'INTERNAL'}
    ])
    (flexmock(session)
     .should_receive('getRPMHeaders')
     .and_return({'SOURCERPM': 'foobar-1-1.src.rpm'}))
    (flexmock(session)
     .should_receive('getBuild')
     .with_args(KOJI_BUILD['build_id'], strict=True)
     .and_return(KOJI_BUILD))
    (flexmock(session)
     .should_receive('getBuild')
     .with_args(KOJI_BUILD['nvr'], strict=True)
     .and_return(KOJI_BUILD))
    (flexmock(session)
     .should_receive('getBuild')
     .with_args(KOJI_PARENT_BUILD['build_id'], strict=True)
     .and_return(KOJI_PARENT_BUILD))
    flexmock(session).should_receive('krb_login').and_return(True)
    flexmock(koji).should_receive('ClientSession').and_return(session)
    return session


def get_srpm_url(sign_key=None, srpm_filename_override=None):
    base = '{}/packages/{}/{}/{}'.format(KOJI_ROOT, KOJI_BUILD['name'], KOJI_BUILD['version'],
                                         KOJI_BUILD['release'])
    filename = srpm_filename_override or '{}.src.rpm'.format(KOJI_BUILD['nvr'])
    if not sign_key:
        return '{}/src/{}'.format(base, filename)
    else:
        return '{}/data/signed/{}/src/{}'.format(base, sign_key, filename)


def get_remote_url(koji_build):
    base = '{}/packages/{}/{}/{}'.format(KOJI_ROOT, koji_build['name'], koji_build['version'],
                                         koji_build['release'])
    return '{}/files/remote-sources/{}'.format(base, REMOTE_SOURCES_FILE)


def mock_koji_manifest_download(requests_mock, retries=0):
    class MockBytesIO(io.BytesIO):
        reads = 0

        def read(self, *args, **kwargs):
            if MockBytesIO.reads < retries:
                MockBytesIO.reads += 1
                raise requests.exceptions.ConnectionError

            return super(MockBytesIO, self).read(*args, **kwargs)

    flexmock(time).should_receive('sleep')
    sign_keys = ['', 'usedKey', 'notUsed']
    bad_keys = ['notUsed']
    urls = [get_srpm_url(k) for k in sign_keys]

    for url in urls:
        if any(k in url for k in bad_keys):
            requests_mock.register_uri('HEAD', url, text='Not Found', status_code=404)
        else:
            requests_mock.register_uri('HEAD', url, content=b'')

            def body_callback(request, context):
                f = MockBytesIO(b"Source RPM")
                return f
            requests_mock.register_uri('GET', url, body=body_callback)

    def body_remote_callback(request, context):
        f = MockBytesIO(b"remote sources content")
        return f
    requests_mock.register_uri('GET', get_remote_url(KOJI_BUILD), body=body_remote_callback)
    requests_mock.register_uri('GET', get_remote_url(KOJI_PARENT_BUILD), body=body_remote_callback)


class TestFetchSources(object):
    @pytest.mark.parametrize('retries', (0, 1, constants.HTTP_MAX_RETRIES + 1))
    @pytest.mark.parametrize('custom_rcm', (None, BASE_CONFIG_MAP))
    @pytest.mark.parametrize('signing_intent', ('unsigned', 'empty', 'one', 'multiple', 'invalid'))
    def test_fetch_sources(self, requests_mock, docker_tasker, koji_session, tmpdir, signing_intent,
                           caplog, retries, custom_rcm):
        mock_koji_manifest_download(requests_mock, retries)
        runner = mock_env(tmpdir, docker_tasker, koji_build_id=1, config_map=custom_rcm,
                          default_si=signing_intent)
        if signing_intent == 'invalid' and not custom_rcm:
            with pytest.raises(PluginFailedException) as exc:
                runner.run()
            msg = 'Could not find files signed by'
            assert msg in str(exc.value)
        elif retries > constants.HTTP_MAX_RETRIES:
            with pytest.raises(PluginFailedException) as exc:
                runner.run()
            msg = "plugin 'fetch_sources' raised an exception:"
            assert msg in str(exc.value)
        else:
            result = runner.run()
            results = result[constants.PLUGIN_FETCH_SOURCES_KEY]
            sources_dir = results['image_sources_dir']
            remote_sources_dir = results['remote_sources_dir']
            orig_build_id = results['sources_for_koji_build_id']
            orig_build_nvr = results['sources_for_nvr']
            sources_list = os.listdir(sources_dir)
            remote_list = set(os.listdir(remote_sources_dir))
            assert orig_build_id == 1
            assert orig_build_nvr == 'foobar-1-1'
            assert len(sources_list) == 1
            assert sources_list[0] == '.'.join([KOJI_BUILD['nvr'], 'src', 'rpm'])
            expected_remotes = set()
            expected_remotes.add('-'.join([KOJI_BUILD['nvr'], REMOTE_SOURCES_FILE]))
            expected_remotes.add('-'.join([KOJI_PARENT_BUILD['nvr'], REMOTE_SOURCES_FILE]))
            assert remote_list == expected_remotes
            with open(os.path.join(sources_dir, sources_list[0]), 'rb') as f:
                assert f.read() == b'Source RPM'
            if signing_intent in ['unsigned, empty']:
                assert get_srpm_url() in caplog.text
            if signing_intent in ['one, multiple']:
                assert get_srpm_url('usedKey') in caplog.text
            if custom_rcm:
                assert get_srpm_url() in caplog.text
                assert get_srpm_url('usedKey') not in caplog.text
            assert runner.workflow.labels['sources_for_nvr'] == 'foobar-1-1'

    @pytest.mark.parametrize('signing_intent', ('unsigned', 'empty', 'one', 'multiple', 'invalid'))
    def test_koji_signing_intent(self, requests_mock, docker_tasker, koji_session, tmpdir,
                                 signing_intent, caplog):
        """Make sure fetch_sources plugin prefers the koji image build signing intent"""
        image_signing_intent = 'unsigned'
        extra_image = {'odcs': {'signing_intent': image_signing_intent}}

        koji_build = deepcopy(KOJI_BUILD)
        koji_build['extra'].update({'image': extra_image})
        flexmock(koji_session).should_receive('getBuild').and_return(koji_build)

        mock_koji_manifest_download(requests_mock)
        runner = mock_env(tmpdir, docker_tasker, koji_build_id=1, default_si=signing_intent)
        result = runner.run()
        sources_dir = result[constants.PLUGIN_FETCH_SOURCES_KEY]['image_sources_dir']
        sources_list = os.listdir(sources_dir)
        assert len(sources_list) == 1
        assert sources_list[0] == '.'.join([KOJI_BUILD['nvr'], 'src', 'rpm'])
        with open(os.path.join(sources_dir, sources_list[0]), 'rb') as f:
            assert f.read() == b'Source RPM'
        assert get_srpm_url() in caplog.text
        if signing_intent == 'invalid':
            msg = 'Could not find files signed by'
            assert msg not in caplog.text
        if signing_intent in ['one, multiple']:
            assert get_srpm_url('usedKey') not in caplog.text
        assert result[constants.PLUGIN_FETCH_SOURCES_KEY]['signing_intent'] == image_signing_intent

    def test_no_build_info(self, requests_mock, docker_tasker, koji_session, tmpdir):
        mock_koji_manifest_download(requests_mock)
        runner = mock_env(tmpdir, docker_tasker)
        with pytest.raises(PluginFailedException) as exc:
            runner.run()
        msg = 'FetchSourcesPlugin expects either koji_build_id or koji_build_nvr to be defined'
        assert msg in str(exc.value)

    @pytest.mark.parametrize('build_id, build_nvr', (('1', None), (None, 1), ('1', 1)))
    def test_build_info_with_wrong_type(self, requests_mock, docker_tasker, koji_session, tmpdir,
                                        build_id, build_nvr):
        mock_koji_manifest_download(requests_mock)
        runner = mock_env(tmpdir, docker_tasker, koji_build_id=build_id, koji_build_nvr=build_nvr)
        with pytest.raises(PluginFailedException) as exc:
            runner.run()
        id_msg = 'koji_build_id must be an int'
        nvr_msg = 'koji_build_nvr must be a str'
        if build_id:
            assert id_msg in str(exc.value)
        if build_nvr:
            assert nvr_msg in str(exc.value)

    @pytest.mark.parametrize('build_nvr', ('foobar-1-1', u'foobar-1-1'))
    def test_build_info_with_unicode(self, requests_mock, docker_tasker, koji_session, tmpdir,
                                     caplog, build_nvr):
        mock_koji_manifest_download(requests_mock)
        runner = mock_env(tmpdir, docker_tasker, koji_build_nvr=build_nvr)
        runner.run()
        nvr_msg = 'koji_build_nvr must be a str'
        assert nvr_msg not in caplog.text

    def test_build_with_nvr(self, requests_mock, docker_tasker, koji_session, tmpdir):
        mock_koji_manifest_download(requests_mock)
        runner = mock_env(tmpdir, docker_tasker, koji_build_nvr='foobar-1-1')
        result = runner.run()
        sources_dir = result[constants.PLUGIN_FETCH_SOURCES_KEY]['image_sources_dir']
        sources_list = os.listdir(sources_dir)
        assert len(sources_list) == 1
        assert os.path.basename(sources_list[0]) == '.'.join([KOJI_BUILD['nvr'], 'src', 'rpm'])

    def test_id_and_nvr(self, requests_mock, docker_tasker, koji_session, tmpdir):
        mock_koji_manifest_download(requests_mock)
        runner = mock_env(tmpdir, docker_tasker, koji_build_nvr='foobar-1-1', koji_build_id=1)
        result = runner.run()
        sources_dir = result[constants.PLUGIN_FETCH_SOURCES_KEY]['image_sources_dir']
        sources_list = os.listdir(sources_dir)
        assert len(sources_list) == 1
        assert os.path.basename(sources_list[0]) == '.'.join([KOJI_BUILD['nvr'], 'src', 'rpm'])

    def test_id_and_nvr_mismatch(self, requests_mock, docker_tasker, koji_session, tmpdir):
        mock_koji_manifest_download(requests_mock)
        runner = mock_env(tmpdir, docker_tasker, koji_build_nvr='foobar-1-1', koji_build_id=2)
        with pytest.raises(PluginFailedException) as exc:
            runner.run()
        msg = 'When specifying both an id and an nvr, they should point to the same image build'
        assert msg in str(exc.value)

    @pytest.mark.parametrize(('build_type', 'koji_build_nvr', 'source_build'), [
        (['rpm', 'operator-manifests'], 'foobar-1-1', False),
        (['module', 'operator-manifests'], 'foobar-1-1', False),
        (['image', 'operator-manifests'], 'foobar-source-1-1', True),
    ])
    def test_invalid_source_build(self, requests_mock, docker_tasker, koji_session, tmpdir,
                                  build_type, koji_build_nvr, source_build):
        mock_koji_manifest_download(requests_mock)
        runner = mock_env(tmpdir, docker_tasker, koji_build_nvr=koji_build_nvr, koji_build_id=1)

        typeinfo_dict = {b_type: {} for b_type in build_type}
        name, version, release = koji_build_nvr.rsplit('-', 2)
        koji_build = {'build_id': 1, 'nvr': koji_build_nvr, 'name': name, 'version': version,
                      'release': release, 'extra': typeinfo_dict}
        if source_build:
            koji_build['extra']['image'] = {'sources_for_nvr': 'some source'}

        flexmock(koji_session).should_receive('getBuild').and_return(koji_build)

        with pytest.raises(PluginFailedException) as exc:
            runner.run()

        if 'image' not in build_type:
            msg = ('koji build {} is not image build which source container requires'
                   .format(koji_build_nvr))
        else:
            msg = ('koji build {} is source container build, source container can not '
                   'use source container build image'.format(koji_build_nvr))

        assert msg in str(exc.value)

    @pytest.mark.parametrize('signing_key', [None, 'usedKey'])
    @pytest.mark.parametrize('srpm_filename', [
        'baz-1-1.src.rpm',
        'baz-2-3.src.rpm',
        'lib-foobar-1-1.src.rpm'
    ])
    def test_rpm_name_different_from_srpm_name(self, requests_mock, docker_tasker, koji_session,
                                               tmpdir, caplog, srpm_filename, signing_key):
        (flexmock(koji_session)
            .should_receive('getRPMHeaders')
            .and_return({'SOURCERPM': srpm_filename}))
        (flexmock(koji_session)
            .should_receive('listArchives')
            .with_args(object, type='remote-sources')
            .and_return([]))

        key = None if signing_key is None else signing_key.lower()
        srpm_url = get_srpm_url(key, srpm_filename_override=srpm_filename)
        requests_mock.register_uri('HEAD', srpm_url)
        requests_mock.register_uri('GET', srpm_url)

        signing_intent = 'one' if signing_key is not None else 'empty'
        runner = mock_env(tmpdir, docker_tasker, koji_build_nvr='foobar-1-1',
                          default_si=signing_intent)
        runner.run()

        assert srpm_url in caplog.text

    @pytest.mark.parametrize(('deny_list', 'denylist_json', 'exc_str'), [
        (None, None, None),
        ({'denylist_url': 'http://denylist_url', 'denylist_key': 'denylist_exists'},
         None,
         'Not Found: http://denylist_url'),
        ({'denylist_url': 'http://denylist_url', 'denylist_key': 'denylist_exists'},
         {'denylist_exists': 'is string'},
         'Denylist value in key: denylist_exists is not list: '),
        ({'denylist_url': 'http://denylist_url', 'denylist_key': 'denylist_exists'},
         {'denylist_exists': ['some', 1, 2, None]},
         'Values in denylist has to be all strings'),
        ({'denylist_url': 'http://denylist_url', 'denylist_key': 'denylist_exists'},
         {'denylist_exists': []},
         None),
        ({'denylist_url': 'http://denylist_url', 'denylist_key': 'denylist_exists'},
         {'denylist_exists': ['foobar']},
         None),
        ({'denylist_url': 'http://denylist_url', 'denylist_key': 'denylist_exists'},
         {'denylist_exists': ['kernel']},
         None),
        ({'denylist_url': 'http://denylist_url', 'denylist_key': 'denylist_exists'},
         {'denylist_exists': ['foobar', 'kernel']},
         None),
        ({'denylist_url': 'http://denylist_url', 'denylist_key': 'denylist_wrong'},
         {'denylist_exists': 'does not matter'},
         'Denylist key: denylist_wrong missing in denylist json from : http://denylist_url')
    ])
    def test_denylist_srpms(self, requests_mock, docker_tasker, koji_session, tmpdir,
                            caplog, deny_list, denylist_json, exc_str):
        rcm_json = yaml.safe_load(BASE_CONFIG_MAP)
        rcm_json['source_container'] = {}

        if deny_list:
            rcm_json['source_container'] = {'denylist_srpms': deepcopy(deny_list)}

        if deny_list and not denylist_json:
            requests_mock.register_uri('GET', deny_list['denylist_url'],
                                       reason='Not Found: {}'.format(deny_list['denylist_url']),
                                       status_code=404)

        elif deny_list and denylist_json:
            requests_mock.register_uri('GET', deny_list['denylist_url'],
                                       json=denylist_json, status_code=200)

        mock_koji_manifest_download(requests_mock)
        koji_build_nvr = 'foobar-1-1'
        runner = mock_env(tmpdir, docker_tasker, koji_build_nvr=koji_build_nvr,
                          config_map=yaml.safe_dump(rcm_json))
        if exc_str:
            with pytest.raises(PluginFailedException) as exc:
                runner.run()
            assert exc_str in str(exc.value)
        else:
            runner.run()

        pkg_name = koji_build_nvr.rsplit('-', 2)[0]
        err_msg = 'skipping denylisted srpm %s' % koji_build_nvr
        if deny_list and exc_str is None and pkg_name in denylist_json['denylist_exists']:
            assert err_msg in caplog.text
        else:
            assert err_msg not in caplog.text

        if deny_list is None:
            assert 'denylist_srpms is not defined in reactor_config_map' in caplog.text
        elif denylist_json and exc_str is None:
            assert 'denylisted srpms: ' in caplog.text

    @pytest.mark.parametrize('use_cache', [True, False, None])
    def test_lookaside_cache(self, requests_mock, docker_tasker, koji_session, tmpdir, use_cache):
        mock_koji_manifest_download(requests_mock)
        koji_build_nvr = 'foobar-1-1'
        runner = mock_env(tmpdir, docker_tasker, koji_build_nvr=koji_build_nvr)

        if use_cache:
            tmpdir.join('sources').write('#ref file.tar.gz')
        elif use_cache is None:
            tmpdir.join('sources').write('')

        err_msg = 'Repository is using lookaside cache, which is not allowed ' \
                  'for source container builds'

        if use_cache:
            with pytest.raises(PluginFailedException) as exc_info:
                runner.run()

            assert err_msg in str(exc_info.value)
        else:
            runner.run()

    @pytest.mark.parametrize('reason', ['external', 'other'])
    def test_missing_srpm_header(self, docker_tasker, koji_session, tmpdir, reason):
        (flexmock(koji_session)
            .should_receive('listArchives')
            .with_args(object, type='image')
            .and_return([{'id': 1}]))
        (flexmock(koji_session)
            .should_receive('listRPMs')
            .with_args(imageID=1)
            .and_return([
                {'id': 1,
                 'build_id': None,
                 'nvr': 'foobar-1-1',
                 'arch': 'x86_64',
                 'external_repo_name': 'some-repo' if reason == 'external' else 'INTERNAL'}
            ]))
        (flexmock(koji_session)
            .should_receive('getRPMHeaders')
            .and_return({}))

        runner = mock_env(tmpdir, docker_tasker, koji_build_nvr='foobar-1-1')
        with pytest.raises(PluginFailedException) as exc_info:
            runner.run()

        if reason == 'external':
            assert 'RPM comes from an external repo' in str(exc_info.value)
        else:
            assert 'Missing SOURCERPM header' in str(exc_info.value)

    def test_no_srpms_and_remote_sources(self, docker_tasker, koji_session, tmpdir):
        (flexmock(koji_session)
            .should_receive('listArchives')
            .with_args(object, type='image')
            .and_return([{'id': 1}]))
        (flexmock(koji_session)
            .should_receive('listArchives')
            .with_args(object, type='remote-sources')
            .and_return([]))
        (flexmock(koji_session)
            .should_receive('listRPMs')
            .with_args(imageID=1)
            .and_return([]))

        runner = mock_env(tmpdir, docker_tasker, koji_build_nvr='foobar-1-1')
        with pytest.raises(PluginFailedException) as exc_info:
            runner.run()

        assert 'No srpms or remote sources found' in str(exc_info.value)
