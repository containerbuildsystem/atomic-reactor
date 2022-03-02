"""
Copyright (c) 2021-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import hashlib
import os
from textwrap import dedent

import pytest
import responses

from atomic_reactor.constants import PLUGIN_FETCH_MAVEN_KEY
from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.post_generate_maven_metadata import GenerateMavenMetadataPlugin, \
    DownloadRequest
from tests.mock_env import MockEnv

FILER_ROOT_DOMAIN = 'filer.com'
FILER_ROOT = 'https://' + FILER_ROOT_DOMAIN

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


def mock_env(workflow):
    r_c_m = {'version': 1,
             'koji': {}}

    env = (MockEnv(workflow)
           .for_plugin('postbuild', GenerateMavenMetadataPlugin.key)
           .make_orchestrator()
           .set_reactor_config(r_c_m))

    workflow.build_dir.init_build_dirs(["aarch64", "x86_64"], workflow.source)

    return env


def mock_source_download_queue(workflow_data, remote_files=None, overrides=None):
    if remote_files is None:
        remote_files = DEFAULT_REMOTE_FILES
    if not overrides:
        overrides = {}

    source_download_queue = []
    source_url_to_artifacts = {}

    for remote_file in remote_files:
        checksums = {algo: remote_file[(algo)] for algo in
                     hashlib.algorithms_guaranteed
                     if algo in remote_file}
        artifact = {
            'url': remote_file['url'],
            'checksums': checksums,
            'filename': os.path.basename(remote_file['url'])
        }
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
        checksums = {algo: remote_file[('source-' + algo)] for algo in
                     hashlib.algorithms_guaranteed
                     if ('source-' + algo) in remote_file}
        target = os.path.basename(url)
        source_download_queue.append(DownloadRequest(url, target, checksums))
        if url not in source_url_to_artifacts:
            source_url_to_artifacts[url] = [artifact]
        else:
            source_url_to_artifacts[url].append(artifact)
    workflow_data.prebuild_results[PLUGIN_FETCH_MAVEN_KEY] = {'source_download_queue':
                                                              source_download_queue,
                                                              'source_url_to_artifacts':
                                                              source_url_to_artifacts}


@responses.activate
def test_generate_maven_metadata(workflow, source_dir):
    mock_source_download_queue(workflow.data)

    results = mock_env(workflow).create_runner().run()

    plugin_result = results[GenerateMavenMetadataPlugin.key]

    remote_source_files = plugin_result.get('remote_source_files')

    for remote_source_file in remote_source_files:
        assert source_dir.joinpath(
            GenerateMavenMetadataPlugin.DOWNLOAD_DIR, remote_source_file['file']
        ).exists()


@pytest.mark.parametrize('contents', (  # noqa
        dedent("""\
        - url: no source url
          md5: cac3a36cfefd5baced859ac3cd9e2329
        """),
))
@responses.activate
def test_generate_maven_metadata_no_source_download_queue(
        workflow, source_dir, caplog, contents
):
    """Throw deprecation warning when no source-url is provided"""
    mock_source_download_queue(workflow.data, remote_files=[])

    mock_env(workflow).create_runner().run()

    msg = '0 url source files to download'
    assert msg in caplog.text


@responses.activate
def test_generate_maven_metadata_url_bad_checksum(workflow, source_dir):
    """Err when downloaded archive from URL has unexpected checksum."""
    mock_source_download_queue(workflow.data, overrides={REMOTE_FILE_SPAM['source-url']:
                                                         {'body': 'corrupted-file'}})

    with pytest.raises(PluginFailedException) as e:
        mock_env(workflow).create_runner().run()

    assert 'does not match expected checksum' in str(e.value)


@responses.activate
def test_generate_maven_metadata_source_url_no_headers(workflow, source_dir):
    """
    Err if headers are not present.
    """
    remote_file = {'url': FILER_ROOT + '/eggs/eggs.jar',
                   'source-url': FILER_ROOT + '/eggs/eggs-sources.tar;a=snapshot;sf=tgz',
                   'md5': 'b1605c846e03035a6538873e993847e5',
                   'source-md5': '927c5b0c62a57921978de1a0421247ea'}
    mock_source_download_queue(workflow.data, remote_files=[remote_file],
                               overrides={remote_file['source-url']: {'head': True}})

    with pytest.raises(PluginFailedException) as e:
        mock_env(workflow).create_runner().run()

    assert 'AttributeError' in str(e.value)


@responses.activate
def test_generate_maven_metadata_source_url_no_filename_in_headers(workflow, source_dir):
    """
    Err if filename not present in content-disposition.
    """
    remote_file = {'url': FILER_ROOT + '/eggs/eggs.jar',
                   'source-url': FILER_ROOT + '/eggs/eggs-sources.tar;a=snapshot;sf=tgz',
                   'md5': 'b1605c846e03035a6538873e993847e5',
                   'source-md5': '418ddd911e816c41483ef82f7c93c2e3'}
    mock_source_download_queue(workflow.data, remote_files=[remote_file],
                               overrides={remote_file['source-url']:
                                          {'headers': {'Content-disposition': 'no filename'},
                                          'head': True}})

    with pytest.raises(PluginFailedException) as e:
        mock_env(workflow).create_runner().run()

    assert 'IndexError' in str(e.value)
