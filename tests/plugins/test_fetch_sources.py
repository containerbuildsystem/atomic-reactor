"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import

import io
import os
from textwrap import dedent

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
from tests.constants import TEST_IMAGE
from tests.stubs import StubInsideBuilder, StubSource


KOJI_HUB = 'http://koji.com/hub'
KOJI_ROOT = 'http://koji.localhost/kojiroot'
KOJI_UPLOAD_TEST_WORKDIR = 'temp_workdir'
KOJI_BUILD = {'build_id': 1, 'nvr': 'foobar-1-1', 'name': 'foobar', 'version': 1, 'release': 1}
constants.HTTP_BACKOFF_FACTOR = 0

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


class MockSource(StubSource):

    def __init__(self, workdir):
        super(MockSource, self).__init__()
        self.workdir = workdir


def mock_workflow(tmpdir, for_orchestrator=False, config_map=None,
                  default_si=DEFAULT_SIGNING_INTENT):
    workflow = DockerBuildWorkflow(
        TEST_IMAGE,
        source={"provider": "git", "uri": "asd"}
    )
    workflow.source = MockSource(str(tmpdir))
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

    runner = PreBuildPluginsRunner(docker_tasker, workflow, plugin_conf)
    return runner


@pytest.fixture()
def koji_session():
    session = flexmock()
    flexmock(session).should_receive('ssl_login').and_return(True)
    flexmock(session).should_receive('listArchives').and_return([{'id': 1}, {'id': 2}])
    flexmock(session).should_receive('listRPMs').with_args(imageID=1).and_return(
        [{'id': 1, 'build_id': 1, 'nvr': 'foobar-1-1', 'arch': 'x86_64'}])
    flexmock(session).should_receive('listRPMs').with_args(imageID=2).and_return(
        [{'id': 2, 'build_id': 1, 'nvr': 'foobar-1-1', 'arch': 'aarch64'}])
    (flexmock(session)
     .should_receive('getRPMHeaders')
     .and_return({'SOURCERPM': 'foobar-1-1.src.rpm'}))
    flexmock(session).should_receive('getBuild').and_return(KOJI_BUILD)
    flexmock(session).should_receive('krb_login').and_return(True)
    flexmock(koji).should_receive('ClientSession').and_return(session)
    return session


def get_srpm_url(sign_key=None):
    base = '{}/packages/{}/{}/{}'.format(KOJI_ROOT, KOJI_BUILD['name'], KOJI_BUILD['version'],
                                         KOJI_BUILD['release'])
    filename = '{}.src.rpm'.format(KOJI_BUILD['nvr'])
    if not sign_key:
        return '{}/src/{}'.format(base, filename)
    else:
        return '{}/data/signed/{}/src/{}'.format(base, sign_key, filename)


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
            orig_build_id = results['sources_for_koji_build_id']
            orig_build_nvr = results['sources_for_nvr']
            sources_list = os.listdir(sources_dir)
            assert orig_build_id == 1
            assert orig_build_nvr == 'foobar-1-1'
            assert len(sources_list) == 1
            assert sources_list[0] == '.'.join([KOJI_BUILD['nvr'], 'src', 'rpm'])
            with open(os.path.join(sources_dir, sources_list[0]), 'rb') as f:
                assert f.read() == b'Source RPM'
            if signing_intent in ['unsigned, empty']:
                assert get_srpm_url() in caplog.text
            if signing_intent in ['one, multiple']:
                assert get_srpm_url('usedKey') in caplog.text
            if custom_rcm:
                assert get_srpm_url() in caplog.text
                assert get_srpm_url('usedKey') not in caplog.text

    @pytest.mark.parametrize('signing_intent', ('unsigned', 'empty', 'one', 'multiple', 'invalid'))
    def test_koji_signing_intent(self, requests_mock, docker_tasker, koji_session, tmpdir,
                                 signing_intent, caplog):
        """Make sure fetch_sources plugin prefers the koji image build signing intent"""
        extra = {'image': {'odcs': {'signing_intent': 'unsigned'}}}
        KOJI_BUILD.update({'extra': extra})
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
