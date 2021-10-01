"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import json
import os
from textwrap import dedent

import koji
import pytest
import responses
import yaml
from flexmock import flexmock

from atomic_reactor.constants import (REPO_FETCH_ARTIFACTS_KOJI,
                                      REPO_FETCH_ARTIFACTS_PNC)
from atomic_reactor.constants import REPO_FETCH_ARTIFACTS_URL
from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.post_generate_maven_metadata import GenerateMavenMetadataPlugin
from tests.mock_env import MockEnv

KOJI_HUB = 'https://koji-hub.com'
KOJI_ROOT = 'https://koji-root.com'

FILER_ROOT_DOMAIN = 'filer.com'
FILER_ROOT = 'https://' + FILER_ROOT_DOMAIN
PNC_ROOT = 'https://pnc-root.com'

DEFAULT_KOJI_BUILD_ID = 472397

DEFAULT_KOJI_BUILD = {
    'build_id': DEFAULT_KOJI_BUILD_ID,
    'id': DEFAULT_KOJI_BUILD_ID,
    'name': 'com.sun.xml.bind.mvn-jaxb-parent',
    'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1',
    'release': '1',
    'version': '2.2.11.4',
}

ARCHIVE_JAXB_SUN_POM = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': '697317209103338c7c841e327bb6e7b0',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4.pom',
    'group_id': 'com.sun.xml.bind',
    'id': 1269850,
    'size': 15320,
    'version': '2.2.11-4'
}

ARCHIVE_JAXB_SUN_JAR = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': '06bae6472e3d1635f0c3b79bd314fdf3',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4.jar',
    'group_id': 'com.sun.xml.bind',
    'id': 1269849,
    'size': 252461,
    'version': '2.2.11-4'
}

ARCHIVE_JAXB_JAVADOC_SUN_JAR = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': '3643ba275364b29117f2bc5f0bcf18d9',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4-javadoc.jar',
    'group_id': 'com.sun.xml.bind',
    'id': 1269848,
    'size': 819956,
    'version': '2.2.11-4'
}

ARCHIVE_JAXB_GLASSFISH_POM = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': 'cc7b7a4d1c33d83fba9adf95226af570',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4.pom',
    'group_id': 'org.glassfish.jaxb',
    'id': 1269791,
    'size': 3092,
    'version': '2.2.11-4'
}

ARCHIVE_JAXB_GLASSFISH_JAR = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': '2ba4912b1a3c699b09ec99e19820fb09',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4.jar',
    'group_id': 'org.glassfish.jaxb',
    'id': 1269790,
    'size': 156400,
    'version': '2.2.11-4'
}

ARCHIVE_JAXB_JAVADOC_GLASSFIX_JAR = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': '69bc6de0a57dd10c7370573a8e76f0b2',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4-javadoc.jar',
    'group_id': 'org.glassfish.jaxb',
    'id': 1269789,
    'size': 524417,
    'version': '2.2.11-4'
}

ARCHIVE_SHA1 = {
    'artifact_id': 'jaxb-sha1',
    'build_id': 472397,
    'checksum': '66bd6b88ba636993ad0fd522cc1254c9ff5f5a1c',
    'checksum_type': koji.CHECKSUM_TYPES['sha1'],
    'filename': 'jaxb-core-2.2.11-4-sha1.jar',
    'group_id': 'org.glassfish.jaxb.sha1',
    'id': 1269792,
    'size': 524417,
    'version': '2.2.11-4'
}

ARCHIVE_SHA256 = {
    'artifact_id': 'jaxb-sha256',
    'build_id': 472397,
    'checksum': 'ca52bcbda16954c9e83e4c0049277ac77f014ecc16a94ed92bc3203fa13aac7d',
    'checksum_type': koji.CHECKSUM_TYPES['sha256'],
    'filename': 'jaxb-core-2.2.11-4-sha256.jar',
    'group_id': 'org.glassfish.jaxb.sha256',
    'id': 1269792,
    'size': 524417,
    'version': '2.2.11-4'
}

# To avoid having to actually download archives during testing,
# the checksum value is based on the mocked download response,
# which is simply the "filename" and "group_id" values.
DEFAULT_ARCHIVES = [
    ARCHIVE_JAXB_SUN_POM,
    ARCHIVE_JAXB_SUN_JAR,
    ARCHIVE_JAXB_JAVADOC_SUN_JAR,
    ARCHIVE_JAXB_GLASSFISH_POM,
    ARCHIVE_JAXB_GLASSFISH_JAR,
    ARCHIVE_JAXB_JAVADOC_GLASSFIX_JAR,
    ARCHIVE_SHA1,
    ARCHIVE_SHA256,
]

REMOTE_FILE_SPAM = {
    'url': FILER_ROOT + '/spam/spam.jar',
    'source-url': FILER_ROOT + '/spam/spam-sources.tar',
    'md5': 'ec61f019a3d0826c04ab20c55462aa24',
    'source-md5': '5d1ab5ae2a84b0f910a0ec549fd9e22b',
}

REMOTE_FILE_BACON = {
    'url': FILER_ROOT + '/bacon/bacon.jar',
    'source-url': FILER_ROOT + '/bacon/bacon-sources.tar',
    'md5': 'b4dbaf349d175aa5bbd5c5d076c00393',
    'source-md5': '0e31f498696b22bcf11cab31576d9bb7',
}

REMOTE_FILE_WITH_TARGET = {
    'url': FILER_ROOT + '/eggs/eggs.jar',
    'source-url': FILER_ROOT + '/eggs/eggs-sources.tar',
    'md5': 'b1605c846e03035a6538873e993847e5',
    'source-md5': '927c5b0c62a57921978de1a0421247ea',
    'target': 'sgge.jar'
}

REMOTE_FILE_SHA1 = {
    'url': FILER_ROOT + '/ham/ham.jar',
    'source-url': FILER_ROOT + '/ham/ham-sources.tar',
    'sha1': 'c4f8d66d78f5ed17299ae88fed9f8a8c6f3c592a',
    'source-sha1': '81a7a10a48f3ca1bc6c3430f3ace043c864e5d68',
}

REMOTE_FILE_SHA256 = {
    'url': FILER_ROOT + '/sausage/sausage.jar',
    'source-url': FILER_ROOT + '/sausage/sausage-sources.tar',
    'sha256': '0da8e7df6c45b1006b10e4d0df5e1a8d5c4dc17c2c9c0ab53c5714dadb705d1c',
    'source-sha256': '808418d3698d00655f71070150350974cdadf823ba2c490c5e254284fc91a1e9'
}

REMOTE_FILE_MULTI_HASH = {
    'url': FILER_ROOT + '/biscuit/biscuit.jar',
    'source-url': FILER_ROOT + '/biscuit/biscuit-sources.tar',
    'sha256': '05892a95a8257a6c51a5ee4ba122e14e9719d7ead3b1d44e7fbea604da2fc8d1',
    'source-sha256': '8c97cd43d4cb77ad7a79d98da57d230bd3ba8b8d8ac0a4c893b0ea0805c1b18c',
    'sha1': '0eb3dc253aeda45e272f07cf6e77fcc8bcf6628a',
    'source-sha1': 'db3a28795e81067dd986aa1e99ea93fbec7a3e58',
    'md5': '24e4dec8666658ec7141738dbde951c5',
    'source-md5': 'a922c6156ce64ae792f994228cb06304',
}

# To avoid having to actually download archives during testing,
# the md5 value is based on the mocked download response,
# which is simply the url.
DEFAULT_REMOTE_FILES = [REMOTE_FILE_SPAM, REMOTE_FILE_BACON, REMOTE_FILE_WITH_TARGET,
                        REMOTE_FILE_SHA1, REMOTE_FILE_SHA256, REMOTE_FILE_MULTI_HASH]

ARTIFACT_MD5 = {
    'build_id': '12',
    'artifacts': [
        {
            'id': '122',
            'target': 'md5.jar'
        }
    ]
}

ARTIFACT_SHA1 = {
    'build_id': '12',
    'artifacts': [
        {
            'id': '123',
            'target': 'sha1.jar'
        }
    ]
}

ARTIFACT_SHA256 = {
    'build_id': '12',
    'artifacts': [
        {
            'id': '124',
            'target': 'sha256.jar'
        }
    ]
}

ARTIFACT_MULTI_HASH = {
    'build_id': '12',
    'artifacts': [
        {
            'id': '125',
            'target': 'multi-hash.jar'
        }
    ]
}

RESPONSE_MD5 = {
    'id': '122',
    'publicUrl': FILER_ROOT + '/md5.jar',
    'md5': '900150983cd24fb0d6963f7d28e17f72'
}

RESPONSE_SHA1 = {
    'id': '123',
    'publicUrl': FILER_ROOT + '/sha1.jar',
    'sha1': 'a9993e364706816aba3e25717850c26c9cd0d89d'
}

RESPONSE_SHA256 = {
    'id': '124',
    'publicUrl': FILER_ROOT + '/sha256.jar',
    'sha256': 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
}

RESPONSE_MULTI_HASH = {
    'id': '125',
    'publicUrl': FILER_ROOT + '/multi-hash.jar',
    'sha256': 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
    'sha1': 'a9993e364706816aba3e25717850c26c9cd0d89d',
    'md5': '900150983cd24fb0d6963f7d28e17f72'
}

DEFAULT_PNC_ARTIFACTS = {'builds': [ARTIFACT_MD5, ARTIFACT_SHA1, ARTIFACT_SHA256,
                                    ARTIFACT_MULTI_HASH]}

DEFAULT_PNC_RESPONSES = {
    RESPONSE_MD5['id']: RESPONSE_MD5,
    RESPONSE_SHA1['id']: RESPONSE_SHA1,
    RESPONSE_SHA256['id']: RESPONSE_SHA256,
    RESPONSE_MULTI_HASH['id']: RESPONSE_MULTI_HASH
}


class MockSource(object):
    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = os.path.join(tmpdir, 'Dockerfile')
        self.path = tmpdir

    def get_build_file_path(self):
        return self.dockerfile_path, self.path


class MockedPathInfo(object):
    def __init__(self, topdir=None):
        self.topdir = topdir

    def mavenbuild(self, build):
        return '{topdir}/packages/{name}/{version}/{release}/maven'.format(topdir=self.topdir,
                                                                           **build)

    def mavenfile(self, maveninfo):
        return '{group_id}/{artifact_id}/{version}/{filename}'.format(**maveninfo)


def mock_env(tmpdir):
    r_c_m = {'version': 1,
             'koji': {
                 'hub_url': KOJI_HUB,
                 'root_url': KOJI_ROOT,
                 'auth': {}
             }}

    env = (MockEnv()
           .for_plugin('postbuild', GenerateMavenMetadataPlugin.key)
           .make_orchestrator()
           .set_reactor_config(r_c_m))

    env.workflow.source = MockSource(tmpdir)

    return env


def mock_koji_session(koji_proxyuser=None, koji_ssl_certs_dir=None,
                      koji_krb_principal=None, koji_krb_keytab=None,
                      build_info=None, archives=None):
    if not build_info:
        build_info = DEFAULT_KOJI_BUILD
    if not archives:
        archives = DEFAULT_ARCHIVES

    flexmock(koji, PathInfo=MockedPathInfo)

    session = flexmock()

    (flexmock(koji)
     .should_receive('ClientSession')
     .once()
     .and_return(session))

    def mock_get_build(nvr):
        if nvr == DEFAULT_KOJI_BUILD['nvr']:
            return DEFAULT_KOJI_BUILD
        else:
            return None

    (session
     .should_receive('getBuild')
     .replace_with(mock_get_build))

    (session
     .should_receive('listArchives')
     .and_return(archives))

    (session
     .should_receive('krb_login')
     .and_return(True))
    return session


def mock_fetch_artifacts_by_nvr(tmpdir, contents=None):
    if contents is None:
        contents = dedent("""\
            - nvr: com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1
            """)

    tmpdir.join(REPO_FETCH_ARTIFACTS_KOJI).write_text(contents, 'utf-8')


def mock_nvr_downloads(build_info=None, archives=None, overrides=None):
    if not build_info:
        build_info = DEFAULT_KOJI_BUILD
    if not archives:
        archives = DEFAULT_ARCHIVES
    if not overrides:
        overrides = {}

    pi = koji.PathInfo(topdir=KOJI_ROOT)

    for archive in archives:
        url = pi.mavenbuild(build_info) + '/' + pi.mavenfile(archive)
        # Use any overrides for this archive ID
        archive_overrides = overrides.get(archive['id'], {})
        status = archive_overrides.get('status', 200)
        body = archive_overrides.get('body', archive['filename'] + archive['group_id'])
        responses.add(responses.GET, url, body=body, status=status)


def mock_pnc_downloads(contents=None, pnc_responses=None, overrides=None):
    if not contents:
        contents = DEFAULT_PNC_ARTIFACTS
    if not pnc_responses:
        pnc_responses = DEFAULT_PNC_RESPONSES
    if not overrides:
        overrides = {}

    builds = contents['builds']
    # Use any overrides for these builds
    pnc_artifacts_overrides = overrides.get('builds', {})
    for build in builds:
        for artifact in build['artifacts']:
            api_url = PNC_ROOT + '/artifacts/{}'.format(artifact['id'])
            body = pnc_artifacts_overrides.get('body', b'abc')
            status = pnc_artifacts_overrides.get('status', 200)
            responses.add(responses.GET, api_url, body=json.dumps(pnc_responses[artifact['id']]),
                          status=status)
            responses.add(responses.GET, pnc_responses[artifact['id']]['publicUrl'], body=body,
                          status=status)


def mock_fetch_artifacts_from_pnc(tmpdir, contents=None):
    if contents is None:
        contents = yaml.safe_dump(DEFAULT_PNC_ARTIFACTS)

    with open(os.path.join(tmpdir, REPO_FETCH_ARTIFACTS_PNC), 'w') as f:
        f.write(contents)
        f.flush()


def mock_fetch_artifacts_by_url(tmpdir, contents=None):
    if not contents:
        contents = yaml.safe_dump(DEFAULT_REMOTE_FILES)

    tmpdir.join(REPO_FETCH_ARTIFACTS_URL).write_text(contents, 'utf-8')


def mock_url_downloads(remote_files=None, overrides=None):
    if not remote_files:
        remote_files = DEFAULT_REMOTE_FILES
    if not overrides:
        overrides = {}

    for remote_file in remote_files:
        url = remote_file['source-url']
        # Use any overrides for this url
        remote_file_overrides = overrides.get(url, {})
        body = remote_file_overrides.get('body', url)
        headers = remote_file_overrides.get('headers', {})
        status = remote_file_overrides.get('status', 200)
        head = remote_file_overrides.get('head', False)
        if head:
            responses.add(responses.HEAD, url, body='', status=status,
                          headers=headers)
        responses.add(responses.GET, url, body=body, status=status,
                      headers=headers)


@responses.activate
def test_generate_maven_metadata(tmpdir, user_params):
    mock_koji_session()
    mock_fetch_artifacts_by_nvr(tmpdir)
    mock_nvr_downloads()
    mock_fetch_artifacts_from_pnc(tmpdir)
    mock_pnc_downloads()
    mock_fetch_artifacts_by_url(tmpdir)
    mock_url_downloads()

    results = mock_env(tmpdir).create_runner().run()

    plugin_result = results[GenerateMavenMetadataPlugin.key]

    assert len(plugin_result.get('components')) == len(DEFAULT_ARCHIVES)

    components = {(component['filename'], component['checksum'], component['checksum_type'])
                  for component in plugin_result.get('components')}
    remote_source_files = plugin_result.get('remote_source_files')

    for archive in DEFAULT_ARCHIVES:
        assert (archive['filename'], archive['checksum'],
                koji.CHECKSUM_TYPES[archive['checksum_type']]) in components

    expected_build_ids = set()
    builds = DEFAULT_PNC_ARTIFACTS['builds']
    for build in builds:
        expected_build_ids.add(build['build_id'])

    assert 'pnc_build_metadata' in plugin_result
    assert 'builds' in plugin_result['pnc_build_metadata']

    found_build_ids = set()
    for build in plugin_result['pnc_build_metadata']['builds']:
        found_build_ids.add(build['id'])

    assert expected_build_ids == found_build_ids

    for remote_source_file in remote_source_files:
        dest = os.path.join(str(tmpdir), GenerateMavenMetadataPlugin.DOWNLOAD_DIR,
                            remote_source_file['file'])
        assert os.path.exists(dest)


@pytest.mark.parametrize('contents', (  # noqa
        dedent("""\
        - url: no source url
          md5: cac3a36cfefd5baced859ac3cd9e2329
        """),
))
@responses.activate
def test_generate_maven_metadata_no_source_url(tmpdir, caplog, user_params, contents):
    """Throw deprecation warning when no source-url is provided"""
    mock_koji_session()
    mock_fetch_artifacts_by_url(tmpdir, contents=contents)
    mock_url_downloads()

    mock_env(tmpdir).create_runner().run()

    msg = 'fetch-artifacts-url without source-url is deprecated'
    assert msg in caplog.text


@responses.activate
def test_generate_maven_metadata_url_bad_checksum(tmpdir, user_params):
    """Err when downloaded archive from URL has unexpected checksum."""
    mock_koji_session()
    mock_fetch_artifacts_by_url(tmpdir)
    mock_url_downloads(overrides={REMOTE_FILE_SPAM['source-url']: {'body': 'corrupted-file'}})

    with pytest.raises(PluginFailedException) as e:
        mock_env(tmpdir).create_runner().run()

    assert 'does not match expected checksum' in str(e.value)


@responses.activate
def test_generate_maven_metadata_source_url_no_headers(tmpdir, user_params):
    """
    Err if headers are not present.
    """
    mock_koji_session()
    remote_file = {'url': FILER_ROOT + '/eggs/eggs.jar',
                   'source-url': FILER_ROOT + '/eggs/eggs-sources.tar;a=snapshot;sf=tgz',
                   'md5': 'b1605c846e03035a6538873e993847e5',
                   'source-md5': '927c5b0c62a57921978de1a0421247ea'}
    mock_fetch_artifacts_by_url(tmpdir, contents=yaml.safe_dump([remote_file]))
    mock_url_downloads(remote_files=[remote_file],
                       overrides={remote_file['source-url']: {'head': True}})

    with pytest.raises(PluginFailedException) as e:
        mock_env(tmpdir).create_runner().run()

    assert 'AttributeError' in str(e.value)


@responses.activate
def test_generate_maven_metadata_source_url_no_filename_in_headers(tmpdir, user_params):
    """
    Err if filename not present in content-disposition.
    """
    mock_koji_session()
    remote_file = {'url': FILER_ROOT + '/eggs/eggs.jar',
                   'source-url': FILER_ROOT + '/eggs/eggs-sources.tar;a=snapshot;sf=tgz',
                   'md5': 'b1605c846e03035a6538873e993847e5',
                   'source-md5': '418ddd911e816c41483ef82f7c93c2e3'}
    mock_fetch_artifacts_by_url(tmpdir, contents=yaml.safe_dump([remote_file]))
    mock_url_downloads(remote_files=[remote_file],
                       overrides={remote_file['source-url']:
                                  {'headers': {'Content-disposition': 'no filename'},
                                   'head': True}})

    with pytest.raises(PluginFailedException) as e:
        mock_env(tmpdir).create_runner().run()

    assert 'IndexError' in str(e.value)
