"""
Copyright (c) 2019, 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import io
import os
from pathlib import Path

import yaml
from textwrap import dedent
from copy import deepcopy
import json
import time
import tarfile
import shutil

import atomic_reactor
import koji
import pytest
import requests
from atomic_reactor.constants import (PNC_SYSTEM_USER, REMOTE_SOURCE_TARBALL_FILENAME,
                                      REMOTE_SOURCE_JSON_FILENAME)
from flexmock import flexmock

from atomic_reactor import constants
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_fetch_sources import FetchSourcesPlugin
from atomic_reactor.util import get_checksums

KOJI_HUB = 'http://koji.com/hub'
KOJI_ROOT = 'http://koji.localhost/kojiroot'
PNC_BASE_API_URL = 'http://pnc.localhost/pnc-rest/v2'
PNC_GET_SCM_ARCHIVE_PATH = 'builds/{}/scm-archive'
PNC_GET_ARTIFACT_PATH = 'artifacts/{}'
KOJI_UPLOAD_TEST_WORKDIR = 'temp_workdir'
ALL_ARCHIVE_NAMES = ['remote-source-first.json', 'remote-source-first.tar.gz',
                     'remote-source-second.json', 'remote-source-second.tar.gz']
RS_TYPEINFO = [{'name': 'first', 'url': 'first_url',
                'archives': ['remote-source-first.json', 'remote-source-first.tar.gz']},
               {'name': 'second', 'url': 'second_url',
                'archives': ['remote-source-second.json', 'remote-source-second.tar.gz']}]
RS_TYPEINFO_NO_JSON = [{'name': 'first', 'url': 'first_url',
                        'archives': ['remote-source-first.wrong', 'remote-source-first.tar.gz']},
                       {'name': 'second', 'url': 'second_url',
                        'archives': ['remote-source-second.bad', 'remote-source-second.tar.gz']}]
RS_TYPEINFO_NO_2 = [{'name': 'first', 'url': 'first_url',
                     'archives': ['remote-source-first.tar.gz']},
                    {'name': 'second', 'url': 'second_url',
                     'archives': ['remote-source-second.tar.gz']}]

KOJI_BUILD_WO_RS = {'build_id': 1, 'nvr': 'foobar-1-1', 'name': 'foobar', 'version': 1,
                    'release': 1,
                    'extra': {'image': {'parent_build_id': 10,
                                        'pnc': {'builds': [{'id': 1234}]}},
                              'operator-manifests': {}},
                    'source': 'registry.com/repo#ref'}
KOJI_BUILD_RS = {'build_id': 1, 'nvr': 'foobar-1-1', 'name': 'foobar', 'version': 1, 'release': 1,
                 'extra': {'image': {'parent_build_id': 10,
                                     'pnc': {'builds': [{'id': 1234}]},
                                     'remote_source_url': 'remote_url'},
                           'operator-manifests': {}},
                 'source': 'registry.com/repo#ref'}
KOJI_BUILD_MRS = {'build_id': 1, 'nvr': 'foobar-1-1', 'name': 'foobar', 'version': 1, 'release': 1,
                  'extra': {'image': {'parent_build_id': 10,
                                      'pnc': {'builds': [{'id': 1234}]},
                                      'remote_sources': []},
                            'operator-manifests': {},
                            'typeinfo': {'remote-sources': RS_TYPEINFO}},
                  'source': 'registry.com/repo#ref'}

KOJI_PARENT_BUILD_WO_RS = {'build_id': 10, 'nvr': 'parent-1-1', 'name': 'parent', 'version': 1,
                           'release': 1,
                           'extra': {'image': {},
                                     'operator-manifests': {}}}
KOJI_PARENT_BUILD_RS = {'build_id': 10, 'nvr': 'parent-1-1', 'name': 'parent', 'version': 1,
                        'release': 1,
                        'extra': {'image': {'remote_source_url': 'remote_url'},
                                  'operator-manifests': {}}}
KOJI_PARENT_BUILD_MRS = {'build_id': 10, 'nvr': 'parent-1-1', 'name': 'parent', 'version': 1,
                         'release': 1,
                         'extra': {'image': {'remote_sources': []},
                                   'operator-manifests': {},
                                   'typeinfo': {'remote-sources': RS_TYPEINFO}}}

KOJI_PNC_BUILD = {'build_id': 25, 'nvr': 'foobar-1-1', 'name': 'foobar', 'version': 1, 'release': 1,
                  'extra': {'image': {'parent_build_id': 10}, 'operator-manifests': {},
                            'external_build_id': 1234},
                  'source': 'registry.com/repo#ref', 'owner_name': PNC_SYSTEM_USER}
KOJI_MEAD_BUILD = {'build_id': 26, 'nvr': 'foobar-1-1', 'name': 'foobar', 'version': 1,
                   'release': 1, 'source': 'registry.com/repo#ref', 'owner_name': 'foo',
                   'extra': {'image': {'parent_build_id': 10}, 'operator-manifests': {}}}

constants.HTTP_BACKOFF_FACTOR = 0

REMOTE_SOURCE_FILE_FILENAME = 'pnc-source.tar.gz'
KOJIFILE_PNC_FILENAME = 'kojifile_pnc.jar'
KOJIFILE_MEAD_FILENAME = 'kojifile_mead.jar'
KOJIFILE_PNC_SOURCE_FILENAME = 'pnc-project-sources.tar.gz'
KOJIFILE_MEAD_SOURCE_FILENAME = 'mead-project-sources.tar.gz'

KOJIFILE_MEAD_SOURCE_ARCHIVE = {'id': 27, 'type_name': 'tar',
                                'filename': KOJIFILE_MEAD_SOURCE_FILENAME, 'checksum_type': 0,
                                'version': 1.0, 'build_id': 26, 'group_id': 'foo.bar',
                                'artifact_id': 1}

REMOTE_SOURCE_FILE_ARCHIVE = {'id': 28, 'type_name': 'tar', 'filename': REMOTE_SOURCE_FILE_FILENAME,
                              'checksum_type': 0}

DEFAULT_SIGNING_INTENT = 'empty'

BASE_CONFIG_MAP = dedent("""\
    version: 1
    koji:
       hub_url: {}
       root_url: {}
       auth:
           ssl_certs_dir: not_needed_here
    pnc:
      base_api_url: {}
      get_scm_archive_path: {}
      get_artifact_path: {}
    """.format(KOJI_HUB, KOJI_ROOT, PNC_BASE_API_URL, PNC_GET_SCM_ARCHIVE_PATH,
               PNC_GET_ARTIFACT_PATH))


def mock_reactor_config(workflow, source_dir: Path, data=None, default_si=DEFAULT_SIGNING_INTENT):
    if data is None:
        data = dedent("""\
            version: 1
            koji:
               hub_url: {}
               root_url: {}
               auth:
                   ssl_certs_dir: not_needed_here
            pnc:
              base_api_url: {}
              get_scm_archive_path: {}
              get_artifact_path: {}
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
            """.format(KOJI_HUB, KOJI_ROOT, PNC_BASE_API_URL, PNC_GET_SCM_ARCHIVE_PATH,
                       PNC_GET_ARTIFACT_PATH, default_si, source_dir))

    config = {}
    if data:
        source_dir.joinpath("cert").touch()
        config = yaml.safe_load(data)

    workflow.conf.conf = config


def mock_workflow(workflow, source_dir: Path,
                  for_orchestrator=False, config_map=None,
                  default_si=DEFAULT_SIGNING_INTENT):
    if for_orchestrator:
        workflow.buildstep_plugins_conf = [{'name': constants.PLUGIN_BUILD_ORCHESTRATE_KEY}]

    mock_reactor_config(workflow, source_dir, data=config_map, default_si=default_si)
    return workflow


def mock_env(workflow, source_dir: Path, scratch=False, orchestrator=False, koji_build_id=None,
             koji_build_nvr=None, config_map=None, default_si=DEFAULT_SIGNING_INTENT):
    workflow = mock_workflow(workflow,
                             source_dir,
                             for_orchestrator=orchestrator,
                             config_map=config_map,
                             default_si=default_si)
    plugin_conf = [{'name': FetchSourcesPlugin.key}]
    plugin_conf[0]['args'] = {
        'koji_build_id': koji_build_id,
        'koji_build_nvr': koji_build_nvr
    }

    flexmock(atomic_reactor.source.GitSource, get=str(source_dir))
    runner = PreBuildPluginsRunner(workflow, plugin_conf)
    return runner


@pytest.fixture()
def koji_session():
    session = flexmock()
    flexmock(session).should_receive('ssl_login').and_return(True)
    (flexmock(session)
     .should_receive('listArchives')
     .with_args(object, type='image')
     .and_return([{'id': 1}, {'id': 2}, {'id': 3}]))
    (flexmock(session)
     .should_receive('listArchives')
     .with_args(object, type='remote-sources')
     .and_return([{'id': 1, 'type_name': 'tar', 'filename': REMOTE_SOURCE_TARBALL_FILENAME},
                  {'id': 20, 'type_name': 'json', 'filename': REMOTE_SOURCE_JSON_FILENAME}]))
    (flexmock(session)
     .should_receive('listArchives')
     .with_args(imageID=1, type='maven')
     .and_return([]))
    (flexmock(session)
     .should_receive('listArchives')
     .with_args(imageID=2, type='maven')
     .and_return([]))
    (flexmock(session)
     .should_receive('listArchives')
     .with_args(imageID=3, type='maven')
     .and_return([{'id': 9, 'type_name': 'jar', 'filename': KOJIFILE_PNC_FILENAME,
                   'checksum_type': 0, 'checksum': '7e79f2ea63aadf1948c82bb3bca74f26',
                   'build_id': 25},
                  {'id': 10, 'type_name': 'jar', 'filename': KOJIFILE_MEAD_FILENAME,
                   'checksum_type': 0, 'checksum': '7e79f2ea63aadf1948c82bb3bca74f26',
                   'build_id': 26}
                  ]))
    (flexmock(session)
     .should_receive('listArchives')
     .with_args(buildID=26, type='maven')
     .and_return([KOJIFILE_MEAD_SOURCE_ARCHIVE]))
    (flexmock(session)
     .should_receive('listArchives')
     .with_args(object, type='remote-source-file')
     .and_return([REMOTE_SOURCE_FILE_ARCHIVE]))
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
    flexmock(session).should_receive('listRPMs').with_args(imageID=3).and_return([])
    (flexmock(session)
     .should_receive('getRPMHeaders')
     .and_return({'SOURCERPM': 'foobar-1-1.src.rpm'}))
    (flexmock(session)
     .should_receive('getBuild')
     .with_args(KOJI_BUILD_RS['build_id'], strict=True)
     .and_return(KOJI_BUILD_RS))
    (flexmock(session)
     .should_receive('getBuild')
     .with_args(KOJI_BUILD_RS['nvr'], strict=True)
     .and_return(KOJI_BUILD_RS))
    (flexmock(session)
     .should_receive('getBuild')
     .with_args(KOJI_PARENT_BUILD_RS['build_id'], strict=True)
     .and_return(KOJI_PARENT_BUILD_RS))
    (flexmock(session)
     .should_receive('getBuild')
     .with_args(KOJI_PNC_BUILD['build_id'], strict=True)
     .and_return(KOJI_PNC_BUILD))
    (flexmock(session)
     .should_receive('getBuild')
     .with_args(KOJI_MEAD_BUILD['build_id'], strict=True)
     .and_return(KOJI_MEAD_BUILD))
    flexmock(session).should_receive('krb_login').and_return(True)
    flexmock(koji).should_receive('ClientSession').and_return(session)
    return session


def set_no_remote_source_in_koji_build(koji_session):
    (flexmock(koji_session)
        .should_receive('getBuild')
        .with_args(KOJI_BUILD_WO_RS['build_id'], strict=True)
        .and_return(KOJI_BUILD_WO_RS))
    (flexmock(koji_session)
        .should_receive('getBuild')
        .with_args(KOJI_BUILD_WO_RS['nvr'], strict=True)
        .and_return(KOJI_BUILD_WO_RS))
    (flexmock(koji_session)
        .should_receive('getBuild')
        .with_args(KOJI_PARENT_BUILD_WO_RS['build_id'], strict=True)
        .and_return(KOJI_PARENT_BUILD_WO_RS))


def set_multiple_remote_sources_in_koji_build(koji_session, typeinfo):
    koji_build = deepcopy(KOJI_BUILD_MRS)
    koji_parent_build = deepcopy(KOJI_PARENT_BUILD_MRS)
    rs_typeinfo = deepcopy(typeinfo)
    koji_build['extra']['typeinfo']['remote-sources'] = rs_typeinfo
    koji_parent_build['extra']['typeinfo']['remote-sources'] = rs_typeinfo

    (flexmock(koji_session)
        .should_receive('getBuild')
        .with_args(koji_build['build_id'], strict=True)
        .and_return(koji_build))
    (flexmock(koji_session)
        .should_receive('getBuild')
        .with_args(koji_build['nvr'], strict=True)
        .and_return(koji_build))
    (flexmock(koji_session)
        .should_receive('getBuild')
        .with_args(koji_parent_build['build_id'], strict=True)
        .and_return(koji_parent_build))


def get_srpm_url(sign_key=None, srpm_filename_override=None):
    base = '{}/packages/{}/{}/{}'.format(KOJI_ROOT, KOJI_BUILD_RS['name'], KOJI_BUILD_RS['version'],
                                         KOJI_BUILD_RS['release'])
    filename = srpm_filename_override or '{}.src.rpm'.format(KOJI_BUILD_RS['nvr'])
    if not sign_key:
        return '{}/src/{}'.format(base, filename)
    else:
        return '{}/data/signed/{}/src/{}'.format(base, sign_key, filename)


def get_remote_url(koji_build, file_name=REMOTE_SOURCE_TARBALL_FILENAME):
    base = '{}/packages/{}/{}/{}'.format(KOJI_ROOT, koji_build['name'], koji_build['version'],
                                         koji_build['release'])
    return '{}/files/remote-sources/{}'.format(base, file_name)


def get_kojifile_source_mead_url(koji_build, source_archive):
    base = '{}/packages/{}/{}/{}'.format(KOJI_ROOT, koji_build['name'], koji_build['version'],
                                         koji_build['release'])
    group_path = '/'.join(source_archive['group_id'].split('.'))
    return '{}/maven/{}/{}/{}/{}'.format(base, group_path, source_archive['artifact_id'],
                                         source_archive['version'],
                                         source_archive['filename'])


def get_pnc_api_url(build_id):
    return (PNC_BASE_API_URL + '/' + PNC_GET_SCM_ARCHIVE_PATH).format(build_id)


def get_pnc_source_url():
    return f'http://code.gerrit.localhost/{KOJIFILE_PNC_SOURCE_FILENAME};sf=tgz'


def get_remote_file_url(koji_build, file_name=REMOTE_SOURCE_FILE_FILENAME):
    base = '{}/packages/{}/{}/{}'.format(KOJI_ROOT, koji_build['name'], koji_build['version'],
                                         koji_build['release'])
    return '{}/files/remote-source-file/{}'.format(base, file_name)


def mock_koji_manifest_download(source_dir: Path, requests_mock,
                                retries=0, dirs_in_remote=('app', 'deps'),
                                files_in_remote=(), cachito_package_names=None,
                                change_package_names=True):
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
        f = MockBytesIO(targz_bytes)
        return f

    if 'app' not in dirs_in_remote:
        source_dir.joinpath("app").mkdir()
    if 'deps' not in dirs_in_remote:
        source_dir.joinpath("deps").mkdir()

    for dir_name in dirs_in_remote:
        source_dir.joinpath(dir_name).mkdir()

    for file_name in files_in_remote:
        source_dir.joinpath(file_name).touch()

    with tarfile.open(source_dir / 'test.tar.gz', "w:gz") as tar:
        tar.add(str(source_dir / 'app'), arcname='app')
        tar.add(str(source_dir / 'deps'), arcname='deps')

    shutil.rmtree(str(source_dir / 'app'))
    shutil.rmtree(str(source_dir / 'deps'))

    test_tar = source_dir.joinpath("test.tar.gz")
    targz_bytes = test_tar.read_bytes()
    targz_checksum = get_checksums(str(test_tar), ['md5']).get('md5sum')
    KOJIFILE_MEAD_SOURCE_ARCHIVE['checksum'] = targz_checksum
    REMOTE_SOURCE_FILE_ARCHIVE['checksum'] = targz_checksum

    test_tar.unlink()

    def body_remote_json_callback(request, context):
        remote_json = {'packages': []}
        if cachito_package_names:
            for pkg in cachito_package_names:
                if change_package_names:
                    remote_json['packages'].append({'name': os.path.join('github.com', pkg)})
                else:
                    remote_json['packages'].append({'name': pkg})
        remote_cont = json.dumps(remote_json)

        remote_bytes = bytes(remote_cont, 'ascii')
        f = io.BytesIO(remote_bytes)
        return f

    for archive in [REMOTE_SOURCE_TARBALL_FILENAME, REMOTE_SOURCE_JSON_FILENAME]:
        body_callback = body_remote_callback
        if archive.endswith('json'):
            body_callback = body_remote_json_callback

        requests_mock.register_uri('GET', get_remote_url(KOJI_BUILD_RS, file_name=archive),
                                   body=body_callback)
        requests_mock.register_uri('GET', get_remote_url(KOJI_PARENT_BUILD_RS, file_name=archive),
                                   body=body_callback)

    for archive in ALL_ARCHIVE_NAMES:
        body_callback = body_remote_callback
        if archive.endswith('json'):
            body_callback = body_remote_json_callback

        requests_mock.register_uri('GET', get_remote_url(KOJI_BUILD_MRS, file_name=archive),
                                   body=body_callback)
        requests_mock.register_uri('GET', get_remote_url(KOJI_PARENT_BUILD_MRS, file_name=archive),
                                   body=body_callback)

    requests_mock.register_uri('GET', get_pnc_source_url(),
                               body=body_remote_callback)
    requests_mock.register_uri('HEAD', get_pnc_source_url(),
                               body='',
                               headers={'Content-disposition':
                                        'inline; filename="{}"'
                               .format(KOJIFILE_PNC_SOURCE_FILENAME)})
    requests_mock.register_uri('GET', get_pnc_api_url(KOJI_PNC_BUILD['extra']['external_build_id']),
                               headers={'Location': get_pnc_source_url()},
                               body=body_remote_callback,
                               status_code=302)
    requests_mock.register_uri('GET', get_kojifile_source_mead_url(KOJI_MEAD_BUILD,
                                                                   KOJIFILE_MEAD_SOURCE_ARCHIVE),
                               body=body_remote_callback)
    requests_mock.register_uri('GET', get_remote_file_url(KOJI_BUILD_RS),
                               body=body_remote_callback)
    requests_mock.register_uri('GET', get_remote_file_url(KOJI_PARENT_BUILD_RS),
                               body=body_remote_callback)


@pytest.mark.usefixtures('user_params')
class TestFetchSources(object):
    @pytest.mark.parametrize('retries', (0, 1, constants.HTTP_MAX_RETRIES + 1))
    @pytest.mark.parametrize('custom_rcm', (None, BASE_CONFIG_MAP))
    @pytest.mark.parametrize('signing_intent', ('unsigned', 'empty', 'one', 'multiple', 'invalid'))
    def test_fetch_sources_remote_source(self, requests_mock, koji_session, source_dir, workflow,
                                         signing_intent, caplog, retries, custom_rcm):
        mock_koji_manifest_download(source_dir, requests_mock, retries)
        runner = mock_env(workflow,
                          source_dir,
                          koji_build_id=1,
                          config_map=custom_rcm,
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
            maven_sources_dir = results['maven_sources_dir']
            orig_build_id = results['sources_for_koji_build_id']
            orig_build_nvr = results['sources_for_nvr']
            sources_list = os.listdir(sources_dir)
            remote_list = set(os.listdir(remote_sources_dir))
            maven_list = set()
            for maven_sources_subdir in os.listdir(maven_sources_dir):
                for source_archive in os.listdir(os.path.join(maven_sources_dir,
                                                              maven_sources_subdir)):
                    maven_list.add(source_archive.split('__')[-1])
            assert orig_build_id == 1
            assert orig_build_nvr == 'foobar-1-1'
            assert len(sources_list) == 1
            assert sources_list[0] == '.'.join([KOJI_BUILD_RS['nvr'], 'src', 'rpm'])
            expected_remotes = set()
            expected_remotes.add('-'.join([KOJI_BUILD_RS['nvr'], REMOTE_SOURCE_TARBALL_FILENAME]))
            expected_remotes.add('-'.join([KOJI_PARENT_BUILD_RS['nvr'],
                                           REMOTE_SOURCE_TARBALL_FILENAME]))
            assert remote_list == expected_remotes
            maven_source_archives = set()
            maven_source_archives.add(KOJIFILE_MEAD_SOURCE_FILENAME)
            maven_source_archives.add(KOJIFILE_PNC_SOURCE_FILENAME)
            maven_source_archives.add(REMOTE_SOURCE_FILE_FILENAME)
            assert maven_list == maven_source_archives

            with open(os.path.join(sources_dir, sources_list[0]), 'rb') as f:
                assert f.read() == b'Source RPM'
            if signing_intent in ['unsigned, empty']:
                assert get_srpm_url() in caplog.text
            if signing_intent in ['one, multiple']:
                assert get_srpm_url('usedKey') in caplog.text
            if custom_rcm:
                assert get_srpm_url() in caplog.text
                assert get_srpm_url('usedKey') not in caplog.text
            assert runner.workflow.data.labels['sources_for_koji_build_id'] == 1

    @pytest.mark.parametrize('typeinfo_rs', (RS_TYPEINFO, RS_TYPEINFO_NO_JSON, RS_TYPEINFO_NO_2))
    @pytest.mark.parametrize('archives_in_koji', (4, 3, 5))
    def test_fetch_sources_multiple_remote_sources(self, typeinfo_rs, archives_in_koji,
                                                   workflow, source_dir, caplog,
                                                   requests_mock, koji_session):

        set_multiple_remote_sources_in_koji_build(koji_session, typeinfo=typeinfo_rs)
        missing_archive = None
        extra_archive = None
        all_archives = deepcopy(ALL_ARCHIVE_NAMES)
        should_fail = True
        if typeinfo_rs == RS_TYPEINFO and archives_in_koji == 4:
            should_fail = False

        if archives_in_koji == 3:
            missing_archive = all_archives.pop()
        elif archives_in_koji == 5:
            extra_archive = 'remote-source-extra.tar.gz'
            all_archives.append(extra_archive)

        list_archives = []
        for n, archive in enumerate(all_archives):
            type_name = 'tar'
            if archive.endswith('json'):
                type_name = 'json'
            list_archives.append({'id': n, 'type_name': type_name, 'filename': archive})

        (flexmock(koji_session)
         .should_receive('listArchives')
         .with_args(object, type='remote-sources')
         .and_return(list_archives))

        mock_koji_manifest_download(source_dir, requests_mock)
        runner = mock_env(workflow, source_dir, koji_build_id=1, config_map=BASE_CONFIG_MAP)

        exc_message = ""
        caplog_message = ""
        if typeinfo_rs == RS_TYPEINFO_NO_2:
            exc_message = 'Problems with archives in remote sources: {}'.format(typeinfo_rs)
            caplog_message = ' does not contain 2 archives, but '

        elif typeinfo_rs == RS_TYPEINFO_NO_JSON:
            exc_message = 'Problems with archives in remote sources: {}'.format(typeinfo_rs)
            caplog_message = 'remote source json, for remote source '

        else:
            if archives_in_koji == 5:
                exc_message = 'Remote source archives in koji missing from ' \
                              'metadata: {}'.format([extra_archive])

            elif archives_in_koji == 3:
                exc_message = 'Remote source files from metadata missing in koji ' \
                              'archives: {}'.format([missing_archive])

        if should_fail:
            with pytest.raises(PluginFailedException) as exc:
                runner.run()
            msg = "plugin 'fetch_sources' raised an exception:"
            assert msg in str(exc.value)

            assert exc_message in str(exc.value)
            if caplog_message:
                assert caplog_message in caplog.text
        else:
            result = runner.run()
            results = result[constants.PLUGIN_FETCH_SOURCES_KEY]
            sources_dir = results['image_sources_dir']
            remote_sources_dir = results['remote_sources_dir']
            maven_sources_dir = results['maven_sources_dir']
            orig_build_id = results['sources_for_koji_build_id']
            orig_build_nvr = results['sources_for_nvr']
            sources_list = os.listdir(sources_dir)
            remote_list = set(os.listdir(remote_sources_dir))
            maven_list = set()
            for maven_sources_subdir in os.listdir(maven_sources_dir):
                for source_archive in os.listdir(os.path.join(maven_sources_dir,
                                                              maven_sources_subdir)):
                    maven_list.add(source_archive.split('__')[-1])
            assert orig_build_id == 1
            assert orig_build_nvr == 'foobar-1-1'
            assert len(sources_list) == 1
            assert sources_list[0] == '.'.join([KOJI_BUILD_RS['nvr'], 'src', 'rpm'])
            expected_remotes = set()
            for archive in ALL_ARCHIVE_NAMES:
                if archive.endswith('json'):
                    continue
                expected_remotes.add('-'.join([KOJI_BUILD_MRS['nvr'], archive]))
                expected_remotes.add('-'.join([KOJI_PARENT_BUILD_MRS['nvr'], archive]))

            assert remote_list == expected_remotes
            maven_source_archives = set()
            maven_source_archives.add(KOJIFILE_MEAD_SOURCE_FILENAME)
            maven_source_archives.add(KOJIFILE_PNC_SOURCE_FILENAME)
            maven_source_archives.add(REMOTE_SOURCE_FILE_FILENAME)
            assert maven_list == maven_source_archives

            with open(os.path.join(sources_dir, sources_list[0]), 'rb') as f:
                assert f.read() == b'Source RPM'
            assert runner.workflow.data.labels['sources_for_koji_build_id'] == 1

    @pytest.mark.parametrize('signing_intent', ('unsigned', 'empty', 'one', 'multiple', 'invalid'))
    def test_koji_signing_intent(self, signing_intent,
                                 requests_mock, koji_session,
                                 workflow, source_dir, caplog):
        """Make sure fetch_sources plugin prefers the koji image build signing intent"""
        image_signing_intent = 'unsigned'
        extra_image = {'odcs': {'signing_intent': image_signing_intent}}

        koji_build = deepcopy(KOJI_BUILD_RS)
        koji_build['extra'].update({'image': extra_image})
        flexmock(koji_session).should_receive('getBuild').and_return(koji_build)
        (flexmock(koji_session).should_receive('listArchives')
         .with_args(imageID=3, type='maven')
         .and_return([]))

        mock_koji_manifest_download(source_dir, requests_mock)
        runner = mock_env(workflow, source_dir, koji_build_id=1, default_si=signing_intent)
        result = runner.run()
        sources_dir = result[constants.PLUGIN_FETCH_SOURCES_KEY]['image_sources_dir']
        sources_list = os.listdir(sources_dir)
        assert len(sources_list) == 1
        assert sources_list[0] == '.'.join([KOJI_BUILD_RS['nvr'], 'src', 'rpm'])
        with open(os.path.join(sources_dir, sources_list[0]), 'rb') as f:
            assert f.read() == b'Source RPM'
        assert get_srpm_url() in caplog.text
        if signing_intent == 'invalid':
            msg = 'Could not find files signed by'
            assert msg not in caplog.text
        if signing_intent in ['one, multiple']:
            assert get_srpm_url('usedKey') not in caplog.text
        assert result[constants.PLUGIN_FETCH_SOURCES_KEY]['signing_intent'] == image_signing_intent

    def test_no_build_info(self, requests_mock, koji_session, workflow, source_dir):
        mock_koji_manifest_download(source_dir, requests_mock)
        runner = mock_env(workflow, source_dir)
        with pytest.raises(PluginFailedException) as exc:
            runner.run()
        msg = 'FetchSourcesPlugin expects either koji_build_id or koji_build_nvr to be defined'
        assert msg in str(exc.value)

    @pytest.mark.parametrize('build_id, build_nvr', (('1', None), (None, 1), ('1', 1)))
    def test_build_info_with_wrong_type(self, requests_mock, koji_session, workflow, source_dir,
                                        build_id, build_nvr):
        mock_koji_manifest_download(source_dir, requests_mock)
        runner = mock_env(workflow, source_dir, koji_build_id=build_id, koji_build_nvr=build_nvr)
        with pytest.raises(PluginFailedException) as exc:
            runner.run()
        id_msg = 'koji_build_id must be an int'
        nvr_msg = 'koji_build_nvr must be a str'
        if build_id:
            assert id_msg in str(exc.value)
        if build_nvr:
            assert nvr_msg in str(exc.value)

    @pytest.mark.parametrize('build_nvr', ('foobar-1-1', u'foobar-1-1'))
    def test_build_info_with_unicode(self, requests_mock, koji_session, workflow, source_dir,
                                     caplog, build_nvr):
        mock_koji_manifest_download(source_dir, requests_mock)
        runner = mock_env(workflow, source_dir, koji_build_nvr=build_nvr)
        runner.run()
        nvr_msg = 'koji_build_nvr must be a str'
        assert nvr_msg not in caplog.text

    def test_build_with_nvr(self, requests_mock, koji_session, workflow, source_dir):
        mock_koji_manifest_download(source_dir, requests_mock)
        runner = mock_env(workflow, source_dir, koji_build_nvr='foobar-1-1')
        result = runner.run()
        sources_dir = result[constants.PLUGIN_FETCH_SOURCES_KEY]['image_sources_dir']
        sources_list = os.listdir(sources_dir)
        assert len(sources_list) == 1
        assert os.path.basename(sources_list[0]) == '.'.join([KOJI_BUILD_RS['nvr'], 'src', 'rpm'])

    def test_id_and_nvr(self, requests_mock, koji_session, workflow, source_dir):
        mock_koji_manifest_download(source_dir, requests_mock)
        runner = mock_env(workflow, source_dir, koji_build_nvr='foobar-1-1', koji_build_id=1)
        result = runner.run()
        sources_dir = result[constants.PLUGIN_FETCH_SOURCES_KEY]['image_sources_dir']
        sources_list = os.listdir(sources_dir)
        assert len(sources_list) == 1
        assert os.path.basename(sources_list[0]) == '.'.join([KOJI_BUILD_RS['nvr'], 'src', 'rpm'])

    def test_id_and_nvr_mismatch(self, requests_mock, koji_session, workflow, source_dir):
        mock_koji_manifest_download(source_dir, requests_mock)
        runner = mock_env(workflow, source_dir, koji_build_nvr='foobar-1-1', koji_build_id=2)
        with pytest.raises(PluginFailedException) as exc:
            runner.run()
        msg = 'When specifying both an id and an nvr, they should point to the same image build'
        assert msg in str(exc.value)

    @pytest.mark.parametrize(('build_type', 'koji_build_nvr', 'source_build'), [
        (['rpm', 'operator-manifests'], 'foobar-1-1', False),
        (['module', 'operator-manifests'], 'foobar-1-1', False),
        (['image', 'operator-manifests'], 'foobar-source-1-1', True),
    ])
    def test_invalid_source_build(self, requests_mock, koji_session, workflow, source_dir,
                                  build_type, koji_build_nvr, source_build):
        mock_koji_manifest_download(source_dir, requests_mock)
        runner = mock_env(workflow, source_dir, koji_build_nvr=koji_build_nvr, koji_build_id=1)

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

    def test_no_source_archive_for_mead_build(
        self, requests_mock, koji_session, workflow, source_dir
    ):
        mock_koji_manifest_download(source_dir, requests_mock)
        (flexmock(koji_session)
         .should_receive('listArchives')
         .with_args(buildID=26, type='maven')
         .and_return([]))

        with pytest.raises(PluginFailedException) as exc:
            mock_env(workflow, source_dir, koji_build_id=KOJI_MEAD_BUILD['build_id']).run()

        msg = f"No sources found for {KOJI_MEAD_BUILD['nvr']}"

        assert msg in str(exc.value)

    def test_no_pnc_config_for_pnc_build(self, requests_mock, koji_session, workflow, source_dir):
        mock_koji_manifest_download(source_dir, requests_mock)

        r_c_m = dedent("""\
            version: 1
            koji:
               hub_url: {}
               root_url: {}
               auth:
                   ssl_certs_dir: not_needed_here
            """.format(KOJI_HUB, KOJI_ROOT))

        with pytest.raises(PluginFailedException) as exc:
            env = mock_env(workflow,
                           source_dir,
                           koji_build_id=KOJI_PNC_BUILD['build_id'],
                           config_map=r_c_m)
            env.run()

        msg = 'No PNC configuration found in reactor config map'

        assert msg in str(exc.value)

    @pytest.mark.parametrize('signing_key', [None, 'usedKey'])
    @pytest.mark.parametrize('srpm_filename', [
        'baz-1-1.src.rpm',
        'baz-2-3.src.rpm',
        'lib-foobar-1-1.src.rpm'
    ])
    def test_rpm_name_different_from_srpm_name(
        self, signing_key, srpm_filename, requests_mock, koji_session, workflow, source_dir, caplog
    ):
        set_no_remote_source_in_koji_build(koji_session)
        (flexmock(koji_session)
            .should_receive('getRPMHeaders')
            .and_return({'SOURCERPM': srpm_filename}))
        (flexmock(koji_session)
            .should_receive('listArchives')
            .with_args(object, type='remote-sources')
            .and_return([]))
        (flexmock(koji_session)
            .should_receive('listArchives')
            .with_args(imageID=3, type='maven')
            .and_return([]))
        (flexmock(koji_session)
         .should_receive('listArchives')
         .with_args(object, type='remote-source-file')
         .and_return([]))
        koji_temp_build = deepcopy(KOJI_BUILD_WO_RS)
        del koji_temp_build['extra']['image']['pnc']
        (flexmock(koji_session)
         .should_receive('getBuild')
         .with_args(KOJI_BUILD_WO_RS['nvr'], strict=True)
         .and_return(koji_temp_build))

        key = None if signing_key is None else signing_key.lower()
        srpm_url = get_srpm_url(key, srpm_filename_override=srpm_filename)
        requests_mock.register_uri('HEAD', srpm_url)
        requests_mock.register_uri('GET', srpm_url)

        signing_intent = 'one' if signing_key is not None else 'empty'
        runner = mock_env(workflow,
                          source_dir,
                          koji_build_nvr='foobar-1-1',
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
    def test_denylist_srpms(self, requests_mock, koji_session, workflow, source_dir,
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

        mock_koji_manifest_download(source_dir, requests_mock)
        koji_build_nvr = 'foobar-1-1'
        runner = mock_env(workflow,
                          source_dir,
                          koji_build_nvr=koji_build_nvr,
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
    def test_lookaside_cache(self, requests_mock, koji_session, workflow, source_dir, use_cache):
        mock_koji_manifest_download(source_dir, requests_mock)
        koji_build_nvr = 'foobar-1-1'
        runner = mock_env(workflow, source_dir, koji_build_nvr=koji_build_nvr)

        if use_cache:
            source_dir.joinpath("sources").write_text("#ref file.tar.gz", "utf-8")
        elif use_cache is None:
            source_dir.joinpath("sources").touch()

        err_msg = 'Repository is using lookaside cache, which is not allowed ' \
                  'for source container builds'

        if use_cache:
            with pytest.raises(PluginFailedException) as exc_info:
                runner.run()

            assert err_msg in str(exc_info.value)
        else:
            runner.run()

    @pytest.mark.parametrize('reason', ['external', 'other'])
    def test_missing_srpm_header(self, koji_session, workflow, source_dir, reason):
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

        runner = mock_env(workflow, source_dir, koji_build_nvr='foobar-1-1')
        with pytest.raises(PluginFailedException) as exc_info:
            runner.run()

        if reason == 'external':
            assert 'RPM comes from an external repo' in str(exc_info.value)
        else:
            assert 'Missing SOURCERPM header' in str(exc_info.value)

    def test_no_srpms_and_remote_sources(self, koji_session, workflow, source_dir):
        set_no_remote_source_in_koji_build(koji_session)
        (flexmock(koji_session)
            .should_receive('listArchives')
            .with_args(object, type='image')
            .and_return([{'id': 1}]))
        (flexmock(koji_session)
            .should_receive('listArchives')
            .with_args(object, type='remote-sources')
            .and_return([]))
        (flexmock(koji_session)
            .should_receive('listArchives')
            .with_args(object, type='maven')
            .and_return([]))
        (flexmock(koji_session)
            .should_receive('listArchives')
            .with_args(object, type='remote-source-file')
            .and_return([]))
        (flexmock(koji_session)
            .should_receive('listRPMs')
            .with_args(imageID=1)
            .and_return([]))
        koji_temp_build = deepcopy(KOJI_BUILD_WO_RS)
        del koji_temp_build['extra']['image']['pnc']
        (flexmock(koji_session)
         .should_receive('getBuild')
         .with_args(KOJI_BUILD_WO_RS['nvr'], strict=True)
         .and_return(koji_temp_build))

        runner = mock_env(workflow, source_dir, koji_build_nvr='foobar-1-1')
        with pytest.raises(PluginFailedException) as exc_info:
            runner.run()

        assert 'No srpms or remote sources or maven sources found' in str(exc_info.value)

    @pytest.mark.parametrize(('excludelist', 'excludelist_json', 'cachito_pkg_names',
                              'exclude_messages', 'exc_str'), [
        # test exclude list doesn't exist
        (None, None, None, [], None),
        ({'denylist_sources': 'http://excludelist_url'},
         None,
         None,
         [],
         'Not Found: http://excludelist_url'),

        # test exclude list doesn't match anything
        ({'denylist_sources': 'http://excludelist_url'},
         {'dir1': ['none1', 'none2']},
         None,
         [],
         None),

        # test removing file
        ({'denylist_sources': 'http://excludelist_url'},
         {'dir1': ['toremovefile']},
         None,
         ['Removing excluded file'],
         None),

        ({'denylist_sources': 'http://excludelist_url'},
         {'dir2': ['toremovefile']},
         None,
         [],
         None),

        # test removing directory
        ({'denylist_sources': 'http://excludelist_url'},
         {'dir1': ['toremovedir']},
         None,
         ['Removing excluded directory'],
         None),

        ({'denylist_sources': 'http://excludelist_url'},
         {'dir2': ['toremovedir']},
         None,
         [],
         None),

        # test removing app
        ({'denylist_sources': 'http://excludelist_url'},
         {'dir1': ['appname']},
         [os.path.join('dir1', 'appname')],
         ['Removing app', 'Keeping vendor in app',
          'Package excluded: "{}"'.format(os.path.join('dir1', 'appname'))],
         None),

        ({'denylist_sources': 'http://excludelist_url'},
         {'dir1': ['appname']},
         [os.path.join('github.com', 'dir1', 'appname')],
         ['Removing app', 'Keeping vendor in app',
          'Package excluded: "{}"'.format(os.path.join('github.com', 'dir1', 'appname'))],
         None),

        ({'denylist_sources': 'http://excludelist_url'},
         {'dir1': ['appname']},
         [os.path.join('@dir1', 'appname')],
         ['Removing app', 'Keeping vendor in app',
          'Package excluded: "{}"'.format(os.path.join('@dir1', 'appname'))],
         None),

        ({'denylist_sources': 'http://excludelist_url'},
         {'dir1': ['appname', 'toremovefile']},
         [os.path.join('dir1', 'appname')],
         ['Removing app', 'Removing excluded file', 'Keeping vendor in app',
          'Package excluded: "{}"'.format(os.path.join('dir1', 'appname'))],
         None),

        ({'denylist_sources': 'http://excludelist_url'},
         {'dir1': ['appname', 'toremovefile', 'toremovedir']},
         [os.path.join('dir1', 'appname')],
         ['Removing app', 'Removing excluded file', 'Removing excluded directory',
          'Keeping vendor in app',
          'Package excluded: "{}"'.format(os.path.join('dir1', 'appname'))],
         None),

        ({'denylist_sources': 'http://excludelist_url'},
         {'dir1': ['appname']},
         [os.path.join('dir1', 'appnamepost')],
         [],
         None),

        ({'denylist_sources': 'http://excludelist_url'},
         {'dir1': ['appname']},
         [os.path.join('dir1', 'preappname')],
         [],
         None),

        ({'denylist_sources': 'http://excludelist_url'},
         {'dir1': ['appname']},
         [os.path.join('dir1', 'preappnamepost')],
         [],
         None),
    ])
    @pytest.mark.parametrize('vendor_exists', [True, False])
    @pytest.mark.parametrize(('source_archives', 'source_json', 'raise_early'), [
        (0, 0, None),
        (1, 1, None),
        (0, 1, 'remote source archive missing'),
        (1, 0, 'remote source json missing'),
        (2, 1, 'There can be just one remote sources archive'),
    ])
    def test_exclude_closed_sources(self, requests_mock, koji_session, workflow, source_dir,
                                    caplog, excludelist, excludelist_json, cachito_pkg_names,
                                    exclude_messages, exc_str, vendor_exists, source_archives,
                                    source_json, raise_early):
        list_archives = []
        for n in range(source_archives):
            list_archives.append({'id': n, 'type_name': 'tar',
                                  'filename': REMOTE_SOURCE_TARBALL_FILENAME})
        for n in range(source_json):
            list_archives.append({'id': n, 'type_name': 'json',
                                  'filename': REMOTE_SOURCE_JSON_FILENAME})

        if not source_archives and not source_json:
            set_no_remote_source_in_koji_build(koji_session)

        (flexmock(koji_session)
         .should_receive('listArchives')
         .with_args(object, type='remote-sources')
         .and_return(list_archives))

        rcm_json = yaml.safe_load(BASE_CONFIG_MAP)
        rcm_json['source_container'] = {}

        dirs_to_create = ['app', 'deps',
                          os.path.join('app', 'dir1'),
                          os.path.join('app', 'dir1', 'sub1'),
                          os.path.join('app', 'dir2'),
                          os.path.join('app', 'dir2', 'sub2'),
                          os.path.join('deps', 'dir1'),
                          os.path.join('deps', 'dir2'),
                          os.path.join('deps', 'dir1', 'toremovedir'),
                          os.path.join('deps', 'dir1', 'toremovedir', 'subdir'),
                          os.path.join('deps', 'dir2', 'toremovedirpost'),
                          os.path.join('deps', 'dir2', 'toremovedirpost', 'subdir'),
                          os.path.join('deps', 'dir2', 'pretoremovedir'),
                          os.path.join('deps', 'dir2', 'pretoremovedir', 'subdir'),
                          os.path.join('deps', 'dir2', 'pretoremovedirpost'),
                          os.path.join('deps', 'dir2', 'pretoremovedirpost', 'subdir')]

        files_to_create = [os.path.join('app', 'file1'),
                           os.path.join('app', 'file2'),
                           os.path.join('app', 'dir1', 'file1'),
                           os.path.join('app', 'dir2', 'file2'),
                           os.path.join('deps', 'dir1', 'toremovefile'),
                           os.path.join('deps', 'dir2', 'toremovefilepost'),
                           os.path.join('deps', 'dir2', 'pretoremovefile'),
                           os.path.join('deps', 'dir2', 'pretoremovefilepost')]

        if vendor_exists:
            dirs_to_create.append(os.path.join('app', 'vendor'))
            files_to_create.append(os.path.join('app', 'vendor', 'vendor_file'))

        if excludelist:
            rcm_json['source_container'] = excludelist

        if excludelist and not excludelist_json:
            requests_mock.register_uri('GET', excludelist['denylist_sources'],
                                       reason='Not Found: {}'.format(
                                           excludelist['denylist_sources']),
                                       status_code=404)

        elif excludelist and excludelist_json:
            requests_mock.register_uri('GET', excludelist['denylist_sources'],
                                       json=excludelist_json, status_code=200)

        mock_koji_manifest_download(source_dir,
                                    requests_mock,
                                    dirs_in_remote=dirs_to_create,
                                    files_in_remote=files_to_create,
                                    cachito_package_names=cachito_pkg_names,
                                    change_package_names=False)
        runner = mock_env(workflow, source_dir, koji_build_id=1,
                          config_map=yaml.safe_dump(rcm_json))

        if raise_early:
            with pytest.raises(PluginFailedException) as exc:
                runner.run()
            assert raise_early in str(exc.value)
            return

        if exc_str and source_archives and source_json:
            with pytest.raises(PluginFailedException) as exc:
                runner.run()
            assert exc_str in str(exc.value)
        else:
            result = runner.run()
            results = result[constants.PLUGIN_FETCH_SOURCES_KEY]
            remote_sources_dir = results['remote_sources_dir']

            if source_archives != 1 or source_json != 1:
                assert remote_sources_dir is None
                return

            remote_list = set(os.listdir(remote_sources_dir))
            expected_remotes = set()
            expected_remotes.add('-'.join([KOJI_BUILD_RS['nvr'], REMOTE_SOURCE_TARBALL_FILENAME]))
            expected_remotes.add('-'.join([KOJI_PARENT_BUILD_RS['nvr'],
                                           REMOTE_SOURCE_TARBALL_FILENAME]))
            assert remote_list == expected_remotes

            if not excludelist or not exclude_messages:
                assert "Removing excluded" not in caplog.text
                assert "Package excluded:" not in caplog.text
                assert "Removing app" not in caplog.text
            else:
                for check_msg in exclude_messages:
                    if 'Keeping vendor in app' == check_msg and not vendor_exists:
                        continue
                    assert check_msg in caplog.text
