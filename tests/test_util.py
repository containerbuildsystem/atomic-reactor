"""
Copyright (c) 2015, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import io
import json
import logging
import os
import tempfile
import tarfile
from typing import List

import pytest
import requests
from requests.exceptions import HTTPError, RetryError
import responses
import inspect
import signal
from base64 import b64encode
from collections import namedtuple

from tempfile import mkdtemp
from textwrap import dedent
from flexmock import flexmock

from collections import OrderedDict
import yaml

from atomic_reactor.constants import (IMAGE_TYPE_DOCKER_ARCHIVE, IMAGE_TYPE_OCI, IMAGE_TYPE_OCI_TAR,
                                      MEDIA_TYPE_DOCKER_V2_SCHEMA1, MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                                      MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST,
                                      DOCKERIGNORE, RELATIVE_REPOS_PATH)
from atomic_reactor.inner import BuildResult
from atomic_reactor.util import (LazyGit, figure_out_build_file,
                                 render_yum_repo, process_substitutions,
                                 get_checksums, print_version_of_tools,
                                 get_version_of_tools,
                                 human_size, CommandResult,
                                 registry_hostname, Dockercfg, RegistrySession,
                                 get_manifest_digests, ManifestDigest,
                                 get_manifest_list, get_all_manifests,
                                 get_inspect_for_image, get_manifest,
                                 is_scratch_build, is_isolated_build, is_flatpak_build,
                                 df_parser, base_image_is_custom,
                                 are_plugins_in_order, LabelFormatter,
                                 label_to_string,
                                 guess_manifest_media_type,
                                 get_manifest_media_type,
                                 get_manifest_media_version,
                                 get_primary_images,
                                 get_floating_images,
                                 get_unique_images,
                                 get_image_upload_filename,
                                 read_yaml, read_yaml_from_file_path, read_yaml_from_url,
                                 validate_with_schema,
                                 OSBSLogs,
                                 get_platforms_in_limits, get_orchestrator_platforms,
                                 dump_stacktraces, setup_introspection_signal_handler,
                                 allow_repo_dir_in_dockerignore,
                                 has_operator_appregistry_manifest,
                                 has_operator_bundle_manifest, DockerfileImages,
                                 terminal_key_paths,
                                 map_to_user_params,
                                 create_tar_gz_archive,
                                 )
from tests.constants import MOCK, REACTOR_CONFIG_MAP
import atomic_reactor.util
from atomic_reactor.constants import INSPECT_CONFIG
from atomic_reactor.source import SourceConfig
from osbs.utils import ImageName
from osbs.exceptions import OsbsValidationException
from tests.mock_env import MockEnv
from tests.stubs import StubSource
from tests.constants import OSBS_BUILD_LOG_FILENAME

if MOCK:
    from tests.retry_mock import mock_get_retry_session


class TestCommandResult(object):
    @pytest.mark.parametrize(('item', 'expected'), [
        ({"stream": "Step 0 : FROM ebbc51b7dfa5bcd993a[...]"},
         "Step 0 : FROM ebbc51b7dfa5bcd993a[...]"),

        ('this is not valid JSON',
         'this is not valid JSON'),
    ])
    def test_parse_item(self, item, expected):
        cr = CommandResult()
        cr.parse_item(item)
        assert cr.logs == [expected]


BUILD_FILE_CONTENTS_DOCKER = {
    "Dockerfile": "",
    "container.yaml": "",
    "subdir/Dockerfile": ""
}

BUILD_FILE_CONTENTS_FLATPAK = {
    "container.yaml": "flatpak: {}\n",
    "subdir/container.yaml": "flatpak: {}\n"
}

BUILD_FILE_CONTENTS_BROKEN = {
    "dummy": "",
    "subdir/container.yaml": ""
}


@pytest.mark.parametrize('contents,local_path,expected_path,expected_exception', [
    (BUILD_FILE_CONTENTS_DOCKER, None, "Dockerfile", None),
    (BUILD_FILE_CONTENTS_DOCKER, "subdir", "subdir/Dockerfile", None),
    (BUILD_FILE_CONTENTS_DOCKER, "subdir/Dockerfile", "subdir/Dockerfile", None),
    (BUILD_FILE_CONTENTS_FLATPAK, None, "container.yaml", None),
    (BUILD_FILE_CONTENTS_FLATPAK, "subdir", "subdir/container.yaml", None),
    (BUILD_FILE_CONTENTS_FLATPAK, "subdir/container.yaml", "subdir/container.yaml", None),
    (BUILD_FILE_CONTENTS_BROKEN, None, None, "doesn't exist"),
    (BUILD_FILE_CONTENTS_BROKEN, "subdir", None, "no accompanying Dockerfile"),
    (BUILD_FILE_CONTENTS_BROKEN, "nonexist_subdir", None, "doesn't exist"),
])
def test_figure_out_build_file(tmpdir, contents, local_path, expected_path, expected_exception):
    tmpdir_path = str(tmpdir.realpath())
    for path, path_contents in contents.items():
        fullpath = os.path.join(tmpdir_path, path)
        d = os.path.dirname(fullpath)
        if not os.path.exists(d):
            os.makedirs(d)
        with open(fullpath, "w") as f:
            f.write(path_contents)

    if expected_exception is None:
        path, directory = figure_out_build_file(tmpdir_path, local_path=local_path)
        assert path == os.path.join(tmpdir_path, expected_path)
        assert os.path.isfile(path)
        assert os.path.isdir(directory)
    else:
        with pytest.raises(Exception) as e:
            figure_out_build_file(tmpdir_path, local_path=local_path)
        assert expected_exception in str(e.value)


def test_lazy_git(local_fake_repo):
    lazy_git = LazyGit(git_url=local_fake_repo)
    lazy_git.clone()
    with lazy_git:
        assert lazy_git.git_path is not None
        assert lazy_git.commit_id is not None
        assert len(lazy_git.commit_id) == 40  # current git hashes are this long

        previous_commit_id = lazy_git.commit_id
        lazy_git.reset('HEAD~2')  # Go back two commits
        assert lazy_git.commit_id is not None
        assert lazy_git.commit_id != previous_commit_id
        assert len(lazy_git.commit_id) == 40  # current git hashes are this long


def test_lazy_git_with_tmpdir(local_fake_repo, tmpdir):
    t = str(tmpdir.join("lazy-git-tmp-dir").realpath())
    lazy_git = LazyGit(git_url=local_fake_repo, tmpdir=t)
    lazy_git.clone()
    assert lazy_git._tmpdir == t
    assert lazy_git.git_path is not None
    assert lazy_git.commit_id is not None
    assert len(lazy_git.commit_id) == 40  # current git hashes are this long


def test_render_yum_repo_unicode():
    yum_repo = OrderedDict((
        ("name", "asd"),
        ("baseurl", "http://example.com/$basearch/test.repo"),
        ("enabled", "1"),
        ("gpgcheck", "0"),
    ))
    rendered_repo = render_yum_repo(yum_repo)
    assert rendered_repo == """\
[asd]
name=asd
baseurl=http://example.com/\\$basearch/test.repo
enabled=1
gpgcheck=0
"""


@pytest.mark.parametrize('dct, subst, expected', [
    ({'foo': 'bar'}, ['foo=spam'], {'foo': 'spam'}),
    ({'foo': 'bar'}, ['baz=spam'], {'foo': 'bar', 'baz': 'spam'}),
    ({'foo': 'bar'}, ['foo.bar=spam'], {'foo': {'bar': 'spam'}}),
    ({'foo': 'bar'}, ['spam.spam=spam'], {'foo': 'bar', 'spam': {'spam': 'spam'}}),


    ({'x_plugins': [{'name': 'a', 'args': {'b': 'c'}}]}, {'x_plugins.a.b': 'd'},
        {'x_plugins': [{'name': 'a', 'args': {'b': 'd'}}]}),
    # substituting plugins doesn't add new params
    ({'x_plugins': [{'name': 'a', 'args': {'b': 'c'}}]}, {'x_plugins.a.c': 'd'},
        {'x_plugins': [{'name': 'a', 'args': {'b': 'c'}}]}),
    ({'x_plugins': [{'name': 'a', 'args': {'b': 'c'}}]}, {'x_plugins.X': 'd'},
        ValueError()),
])
def test_process_substitutions(dct, subst, expected):
    if isinstance(expected, Exception):
        with pytest.raises(type(expected)):
            process_substitutions(dct, subst)
    else:
        process_substitutions(dct, subst)
        assert dct == expected


@pytest.mark.parametrize('write_to_file', [True, False])
@pytest.mark.parametrize('content, algorithms, expected, should_fail', [
    (b'abc', ['md5', 'sha256'],
     {'md5sum': '900150983cd24fb0d6963f7d28e17f72',
      'sha256sum': 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'},
     False),
    (b'abc', ['md5'], {'md5sum': '900150983cd24fb0d6963f7d28e17f72'}, False),
    (b'abc', [], {}, False),
    (b'abc', [''], {}, True),
    (b'abc', ['invalid'], {}, True),
    (b'abc', ['md5', 'invalid'], {}, True)
])
def test_get_hexdigests(tmpdir, content, algorithms, expected, write_to_file, should_fail):
    if should_fail:
        with pytest.raises(ValueError):
            checksums = get_checksums('/invalid/path', algorithms)
    else:
        if write_to_file:
            with tempfile.NamedTemporaryFile(dir=str(tmpdir)) as tmpfile:
                tmpfile.write(content)
                tmpfile.flush()
                filename = tmpfile.name
                checksums = get_checksums(filename, algorithms)
        else:
            filename = io.BytesIO(content)
            checksums = get_checksums(filename, algorithms)

        assert checksums == expected


@pytest.mark.parametrize('path, image_type, expected', [
    ('foo.tar', IMAGE_TYPE_DOCKER_ARCHIVE, 'docker-image-XXX.x86_64.tar'),
    ('foo.tar.gz', IMAGE_TYPE_DOCKER_ARCHIVE, 'docker-image-XXX.x86_64.tar.gz'),
    ('foo.tar.gz', IMAGE_TYPE_OCI_TAR, 'oci-image-XXX.x86_64.tar.gz'),
    ('foo', IMAGE_TYPE_OCI, None),
])
def test_get_image_upload_filename(path, image_type, expected):
    metadata = {
        'path': path,
        'type': image_type,
    }
    if expected is None:
        with pytest.raises(ValueError):
            get_image_upload_filename(metadata, 'XXX', 'x86_64')
    else:
        assert get_image_upload_filename(metadata, 'XXX', 'x86_64') == expected


def test_get_versions_of_tools():
    response = get_version_of_tools()
    assert isinstance(response, list)
    for t in response:
        assert t["name"]
        assert t["version"]


def test_print_versions_of_tools():
    print_version_of_tools()


@pytest.mark.parametrize('size_input,expected', [
    (0, "0.00 B"),
    (1, "1.00 B"),
    (-1, "-1.00 B"),
    (1536, "1.50 KiB"),
    (-1024, "-1.00 KiB"),
    (204800, "200.00 KiB"),
    (6983516, "6.66 MiB"),
    (14355928186, "13.37 GiB"),
    (135734710448947, "123.45 TiB"),
    (1180579814801204129310965, "999.99 ZiB"),
    (1074589982539051580812825722, "888.88 YiB"),
    (4223769947617154742438477168, "3493.82 YiB"),
    (-4223769947617154742438477168, "-3493.82 YiB"),
])
def test_human_size(size_input, expected):
    assert human_size(size_input) == expected


@pytest.mark.parametrize(('registry', 'expected'), [
    ('example.com', 'example.com'),
    # things that don't look like URIs are left untouched
    ('example.com/foo', 'example.com/foo'),
    ('http://example.com', 'example.com'),
    ('http://example.com:5000', 'example.com:5000'),
    ('https://example.com:5000', 'example.com:5000'),
    ('https://example.com/foo', 'example.com')
])
def test_registry_hostname(registry, expected):
    assert registry_hostname(registry) == expected


@pytest.mark.parametrize(('config_file_name'), [
    '.dockercfg',
    'dockerconfigjson',
    '',
])
@pytest.mark.parametrize(('config_content'), [
    ({'username': 'john.doe', 'password': 'letmein'}),
    ({'auth': b64encode(b'john.doe:letmein').decode('utf-8')}),
])
@pytest.mark.parametrize(('in_config', 'lookup', 'expected'), [
    ('example.com', 'example.com', True),
    ('example.com', 'https://example.com/v2', True),
    ('https://example.com/v2', 'https://example.com/v2', True),
    ('example.com', 'https://example.com/v2', True),
    ('example.com', 'notexample.com', False),
])
def test_dockercfg(tmpdir, in_config, config_content, lookup, expected, config_file_name):
    temp_dir = mkdtemp(dir=str(tmpdir))
    config_file_path = temp_dir
    if config_file_name:
        config_file_path = os.path.join(temp_dir, '.dockercfg')
        config_file_param = temp_dir
    else:
        config_file_path = os.path.join(temp_dir, 'myconfig.json')
        config_file_param = config_file_path
    with open(config_file_path, 'w+') as dockerconfig:
        dockerconfig.write(json.dumps({
            in_config: config_content
        }))
    if 'auth' in config_content:
        unpacked = Dockercfg(config_file_param).unpack_auth_b64(lookup)
        found = unpacked == ('john.doe:letmein', 'john.doe', 'letmein')
    else:
        creds = Dockercfg(config_file_param).get_credentials(lookup)
        found = creds.get('username') == 'john.doe' and creds.get('password') == 'letmein'

    assert found == expected


def test_missing_dockercfg():
    with pytest.raises(RuntimeError):
        Dockercfg('/this/path/does/not/exist')


@pytest.mark.parametrize(('registry', 'insecure'), [
    ('https://example.com', False),
    ('example.com', True),
    ('example.com', False),
])
@pytest.mark.parametrize(('method', 'responses_method'), [
    (RegistrySession.get, responses.GET),
    (RegistrySession.head, responses.HEAD),
    (RegistrySession.put, responses.PUT),
    (RegistrySession.delete, responses.DELETE),
])
@pytest.mark.parametrize(('config_content'), [
    ({'username': 'john.doe', 'password': 'letmein'}),
    ({'auth': b64encode(b'john.doe:letmein').decode('utf-8')}),
])
@responses.activate
def test_registry_session(tmpdir, registry, insecure, method, responses_method, config_content):
    temp_dir = mkdtemp(dir=str(tmpdir))
    with open(os.path.join(temp_dir, '.dockercfg'), 'w+') as dockerconfig:
        dockerconfig.write(json.dumps({
            registry_hostname(registry): config_content
        }))
    session = RegistrySession(registry, insecure=insecure, dockercfg_path=temp_dir)
    assert session.insecure == insecure
    assert session.dockercfg_path == temp_dir

    path = '/v2/test/image/manifests/latest'
    if registry.startswith('http'):
        url = registry + path
    elif insecure:
        https_url = 'https://' + registry + path
        responses.add(responses_method, https_url, body=requests.ConnectionError())
        url = 'http://' + registry + path
    else:
        url = 'https://' + registry + path

    def request_callback(request, all_headers=True):
        assert request.headers.get('Authorization') is not None
        return (200, {}, 'A-OK')

    responses.add_callback(responses_method, url, request_callback)

    res = method(session, path)
    assert res.text == 'A-OK'


@pytest.mark.parametrize('registry, reactor_config, matched_registry', [

    # Registry is None, have source_registry, should be source_registry
    (None,
     {
         'source_registry': {
            'url': "default_registry.io",
            'auth': {
                'cfg_path': '/not/important'
            }
         }
     },
     {
         'uri': "default_registry.io",
         'insecure': False,
         'dockercfg_path': '/not/important'
     }),
    # Registry is None, have both source_registry and pull_registries, should be source_registry
    (None,
     {
         'source_registry': {
            'url': "default_registry.io",
            'auth': {
                'cfg_path': '/not/important'
            }
         },
         'pull_registries': [
             {
                 'url': "some_registry.io",
                 'insecure': True
             }
         ]
     },
     {
         'uri': "default_registry.io",
         'insecure': False,
         'dockercfg_path': '/not/important'
     }),
    # No source_registry, matches pull_registries
    ('some_registry.io',
     {
         'pull_registries': [
             {
                 'url': "some_registry.io",
                 'insecure': True
             }
         ]
     },
     {
         'uri': "some_registry.io",
         'insecure': True,
         'dockercfg_path': None
     }),
    # No source_registry, matches pull_registries (the hostname part does, not the full URI)
    ('some_registry.io',
     {
         'pull_registries': [
             {
                 'url': "https://some_registry.io",
                 'insecure': False,
                 'auth': {
                     'cfg_path': '/not/important'
                 }
             }
         ]
     },
     {
         'uri': "https://some_registry.io",
         'insecure': False,
         'dockercfg_path': '/not/important'
     }),
    # Have both source_registry and pull_registries, matches pull_registries
    ('some_registry.io',
     {
         'source_registry': {
             'url': "default_registry.io",
             'insecure': False,
             'auth': {
                 'cfg_path': '/not/important'
             }
         },
         'pull_registries': [
             {
                 'url': "some_registry.io",
                 'insecure': False,
                 'auth': {
                     'cfg_path': '/also/not/important'
                 }
             }
         ]
     },
     {
         'uri': "some_registry.io",
         'insecure': False,
         'dockercfg_path': '/also/not/important'
     }),
])
@pytest.mark.parametrize('access', [None, ('pull', 'push')])
def test_registry_create_from_config(workflow, registry, reactor_config, matched_registry, access):
    workflow.conf.conf = reactor_config

    (flexmock(RegistrySession)
     .should_receive('__init__')
     .with_args(matched_registry['uri'],
                insecure=matched_registry['insecure'],
                dockercfg_path=matched_registry['dockercfg_path'],
                access=access))

    RegistrySession.create_from_config(workflow.conf, registry, access)


@pytest.mark.parametrize('registry, reactor_config, error', [
    # Registry not specified, no registries in config
    (None,
     {},
     'No source_registry configured, cannot create default session'),
    # Registry not specified, only pull_registries in config
    (None,
     {
         'pull_registries': [
             {'url': "some_registry.io"}
         ]
     },
     'No source_registry configured, cannot create default session'),
    # Registry specified, no registries in config
    ('some_registry.io',
     {},
     'some_registry.io: No match in pull_registries or source_registry'),
    # Registry specified, no source_registry, does not match pull_registries
    ('some_registry.io',
     {
         'pull_registries': [
             {'url': "some_other_registry.io"}
         ]
     },
     'some_registry.io: No match in pull_registries or source_registry'),
    # Registry specified, no pull_registries, does not match source_registry
    ('some_registry.io',
     {
         'source_registry': {'url': "some_other_registry.io"}
     },
     'some_registry.io: No match in pull_registries or source_registry'),
])
def test_registry_create_from_config_errors(workflow, registry, reactor_config, error):
    workflow.conf.conf = reactor_config

    with pytest.raises(RuntimeError) as exc_info:
        RegistrySession.create_from_config(workflow.conf, registry)

    assert str(exc_info.value) == error


@pytest.mark.parametrize(('version', 'expected'), [
    ('v1', 'application/vnd.docker.distribution.manifest.v1+json'),
    ('v2', 'application/vnd.docker.distribution.manifest.v2+json'),
    ('v2_list', 'application/vnd.docker.distribution.manifest.list.v2+json'),
])
def test_get_manifest_media_type(version, expected):
    assert get_manifest_media_type(version) == expected


def test_get_manifest_media_type_unknown():
    with pytest.raises(RuntimeError):
        assert get_manifest_media_type('no_such_version')


@pytest.mark.parametrize(('content', 'media_type'), [
    (b'{', None),
    (b'{}', None),
    (b'{"\xff', None),
    (b'{"schemaVersion": 1}',
     'application/vnd.docker.distribution.manifest.v1+json'),
    (b'{"schemaVersion": 2}',
     None),
    (b'{"mediaType": "application/vnd.docker.distribution.manifest.v2+json"}',
     'application/vnd.docker.distribution.manifest.v2+json'),
    (b'{"mediaType": "application/vnd.oci.image.manifest.v1"}',
     'application/vnd.oci.image.manifest.v1'),
])
def test_guess_manifest_media_type(content, media_type):
    assert guess_manifest_media_type(content) == media_type


@pytest.mark.parametrize('insecure', [
    True,
    False,
])
@pytest.mark.parametrize('versions,require_digest', [
    (('v1', 'v2', 'v2_list'), True),
    (('v1', 'v2', 'v2_list'), False),
    (('v1',), False),
    (('v1',), True),
    (('v2',), False),
    (('v2',), True),
    (tuple(), False),
    (tuple(), True),
    (None, False),
    (None, True),
    (('v2_list',), True),
    (('v2_list',), False),
])
@pytest.mark.parametrize('creds', [
    ('user1', 'pass'),
    (None, 'pass'),
    ('user1', None),
    None,
])
@pytest.mark.parametrize('image,registry,path', [
    ('not-used.com/spam:latest', 'localhost.com',
     '/v2/spam/manifests/latest'),

    ('not-used.com/food/spam:latest', 'http://localhost.com',
     '/v2/food/spam/manifests/latest'),

    ('not-used.com/spam', 'https://localhost.com',
     '/v2/spam/manifests/latest'),
])
@responses.activate
def test_get_manifest_digests(tmpdir, caplog, image, registry, insecure, creds,
                              versions, require_digest, path):
    kwargs = {}

    image = ImageName.parse(image)
    kwargs['image'] = image

    if creds:
        temp_dir = mkdtemp(dir=str(tmpdir))
        with open(os.path.join(temp_dir, '.dockercfg'), 'w+') as dockerconfig:
            dockerconfig.write(json.dumps({
                registry: {
                    'username': creds[0], 'password': creds[1]
                }
            }))
        kwargs['dockercfg_path'] = temp_dir

    kwargs['registry'] = registry

    if insecure is not None:
        kwargs['insecure'] = insecure

    if versions is not None:
        kwargs['versions'] = versions

    kwargs['require_digest'] = require_digest

    def request_callback(request, all_headers=True):
        if creds and creds[0] and creds[1]:
            assert request.headers['Authorization']

        media_type = request.headers['Accept']
        if media_type.endswith('list.v2+json'):
            digest = 'v2_list-digest'
        elif media_type.endswith('v2+json'):
            digest = 'v2-digest'
        elif media_type.endswith('v1+json'):
            digest = 'v1-digest'
        else:
            raise ValueError('Unexpected media type {}'.format(media_type))

        media_type_prefix = media_type.split('+')[0]
        if all_headers:
            headers = {
                'Content-Type': '{}+jsonish'.format(media_type_prefix),
            }
            if not media_type.endswith('list.v2+json'):
                headers['Docker-Content-Digest'] = digest
        else:
            headers = {}
        return (200, headers, '')

    if registry.startswith('http'):
        url = registry + path
    else:
        # In the insecure case, we should try the https URL, and when that produces
        # an error, fall back to http
        if insecure:
            https_url = 'https://' + registry + path
            responses.add(responses.GET, https_url, body=requests.ConnectionError())
            url = 'http://' + registry + path
        else:
            url = 'https://' + registry + path
    responses.add_callback(responses.GET, url, callback=request_callback)

    expected_versions = versions
    if versions is None:
        # Test default versions value
        expected_versions = ('v1', 'v2')

    expected_result = dict(
        (version, '{}-digest'.format(version))
        for version in expected_versions)
    if versions and 'v2_list' in versions:
        expected_result['v2_list'] = True

    # Only capture errors, since we want to be sure none are reported
    with caplog.at_level(logging.ERROR, logger='atomic_reactor'):
        if expected_versions:
            actual_digests = get_manifest_digests(**kwargs)

            # Check the expected versions are found
            assert actual_digests.v1 == expected_result.get('v1')
            assert actual_digests.v2 == expected_result.get('v2')
            if 'v2_list' in expected_result:
                assert actual_digests.v2_list == expected_result.get('v2_list')
        elif require_digest:
            # When require_digest is set but there is no digest
            # available (no expected_versions), expect a RuntimeError
            with pytest.raises(RuntimeError):
                get_manifest_digests(**kwargs)
        else:
            get_manifest_digests(**kwargs)

    # there should be no errors reported
    assert not caplog.records


@pytest.mark.parametrize('has_content_type_header', [
    True, False
])
@pytest.mark.parametrize('has_content_digest', [
    True, False
])
@pytest.mark.parametrize('manifest_type,can_convert_v2_v1', [
    ('v1', False),
    ('v2', True),
    ('v2', False),
    ('oci', False),
    ('oci_index', False),
])
def test_get_manifest_digests_missing(tmpdir, has_content_type_header, has_content_digest,
                                      manifest_type, can_convert_v2_v1):
    kwargs = {}

    image = ImageName.parse('example.com/spam:latest')
    kwargs['image'] = image

    kwargs['registry'] = 'https://example.com'

    expected_url = 'https://example.com/v2/spam/manifests/latest'

    mock_get_retry_session()

    def custom_get(url, headers, **kwargs):
        assert url == expected_url

        media_type = headers['Accept']
        media_type_prefix = media_type.split('+')[0]

        assert media_type.endswith('+json')

        # Attempt to simulate how a docker registry behaves:
        #  * If the stored digest is v1, return it
        #  * If the stored digest is v2, and v2 is requested, return it
        #  * If the stored digest is v2, and v1 is requested, try
        #    to convert and return v1 or an error.
        if manifest_type == 'v1':
            digest = 'v1-digest'
            media_type_prefix = 'application/vnd.docker.distribution.manifest.v1'
        elif manifest_type == 'v2':
            if media_type_prefix == 'application/vnd.docker.distribution.manifest.v2':
                digest = 'v2-digest'
            else:
                if not can_convert_v2_v1:
                    response_json = {"errors": [{"code": "MANIFEST_INVALID"}]}
                    response = requests.Response()
                    flexmock(response,
                             status_code=400,
                             content=json.dumps(response_json).encode("utf-8"),
                             headers=headers)

                    return response

                digest = 'v1-converted-digest'
                media_type_prefix = 'application/vnd.docker.distribution.manifest.v1'
        elif manifest_type == 'oci':
            if media_type_prefix == 'application/vnd.oci.image.manifest.v1':
                digest = 'oci-digest'
            else:
                headers = {}
                response_json = {"errors": [{"code": "MANIFEST_UNKNOWN"}]}
                response = requests.Response()
                flexmock(response,
                         status_code=requests.codes.not_found,
                         content=json.dumps(response_json).encode("utf-8"),
                         headers=headers)

                return response
        elif manifest_type == 'oci_index':
            if media_type_prefix == 'application/vnd.oci.image.index.v1':
                digest = 'oci-index-digest'
            else:
                headers = {}
                response_json = {"errors": [{"code": "MANIFEST_UNKNOWN"}]}
                response = requests.Response()
                flexmock(response,
                         status_code=requests.codes.not_found,
                         content=json.dumps(response_json).encode("utf-8"),
                         headers=headers)

                return response

        headers = {}
        if has_content_type_header:
            headers['Content-Type'] = '{}+jsonish'.format(media_type_prefix)
        if has_content_digest:
            headers['Docker-Content-Digest'] = digest

        if media_type_prefix == 'application/vnd.docker.distribution.manifest.v1':
            response_json = {'schemaVersion': 1}
        else:
            response_json = {'schemaVersion': 2,
                             'mediaType': media_type_prefix + '+json'}

        response = requests.Response()
        flexmock(response,
                 status_code=200,
                 content=json.dumps(response_json).encode("utf-8"),
                 headers=headers)

        return response

    (flexmock(requests.Session)
        .should_receive('get')
        .replace_with(custom_get))

    actual_digests = get_manifest_digests(**kwargs)
    if manifest_type == 'v1':
        if has_content_digest:
            assert actual_digests.v1 == 'v1-digest'
        else:
            assert actual_digests.v1 is True
        assert actual_digests.v2 is None
        assert actual_digests.oci is None
        assert actual_digests.oci_index is None
    elif manifest_type == 'v2':
        if can_convert_v2_v1:
            if has_content_digest:
                assert actual_digests.v1 == 'v1-converted-digest'
            else:
                assert actual_digests.v1 is True
        else:
            assert actual_digests.v1 is None
        if has_content_digest:
            assert actual_digests.v2 == 'v2-digest'
        else:
            assert actual_digests.v2 is True
        assert actual_digests.oci is None
        assert actual_digests.oci_index is None
    elif manifest_type == 'oci':
        assert actual_digests.v1 is None
        assert actual_digests.v2 is None
        if has_content_digest:
            assert actual_digests.oci == 'oci-digest'
        else:
            assert actual_digests.oci is True
        assert actual_digests.oci_index is None
    elif manifest_type == 'oci_index':
        assert actual_digests.v1 is None
        assert actual_digests.v2 is None
        assert actual_digests.oci is None
        if has_content_digest:
            assert actual_digests.oci_index == 'oci-index-digest'
        else:
            assert actual_digests.oci_index is True


@responses.activate
def test_get_manifest_digests_connection_error(tmpdir):
    # Test that our code to handle falling back from https to http
    # doesn't do anything unexpected when a connection can't be
    # made at all.
    kwargs = {}
    kwargs['image'] = ImageName.parse('example.com/spam:latest')
    kwargs['registry'] = 'https://example.com'

    url = 'https://example.com/v2/spam/manifests/latest'
    responses.add(responses.GET, url, body=requests.ConnectionError())

    with pytest.raises(requests.ConnectionError):
        get_manifest_digests(**kwargs)


@responses.activate
@pytest.mark.parametrize('body', [
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout
])
def test_get_manifest_timeout_error(tmpdir, body):
    image = ImageName.parse('example.com/spam:latest')
    registry = 'https://example.com'
    session = RegistrySession(registry)

    url = 'https://example.com/v2/spam/manifests/latest'
    responses.add(responses.GET, url, body=body())

    with pytest.raises(requests.exceptions.Timeout):
        get_manifest(image, session, 'v2')


@pytest.mark.parametrize('namespace,repo,explicit,expected', [
    ('foo', 'bar', False, 'foo/bar'),
    ('foo', 'bar', True, 'foo/bar'),
    (None, 'bar', False, 'bar'),
    (None, 'bar', True, 'library/bar'),
])
def test_image_name_get_repo(namespace, repo, explicit, expected):
    image = ImageName(namespace=namespace, repo=repo)
    assert image.get_repo(explicit) == expected


def test_get_manifest_media_version_unknown():
    with pytest.raises(RuntimeError):
        assert get_manifest_media_version(ManifestDigest())


@pytest.mark.parametrize('extra_user_params,scratch', [
    ({'scratch': True}, True),
    ({'scratch': False}, False),
    ({}, False),
])
def test_is_scratch_build(workflow, extra_user_params, scratch, user_params):
    workflow.user_params.update(extra_user_params)
    assert is_scratch_build(workflow) == scratch


@pytest.mark.parametrize(('base_image', 'is_custom'), [
    ('fedora', False),
    ('fedora:latest', False),
    ('koji/image-build', True),
    ('koji/image-build:spam.conf', True),
    ('koji/image-build:latest', True),
    ('scratch', False),
])
def test_is_custom_base_build(base_image, is_custom):
    assert base_image_is_custom(base_image) == is_custom


@pytest.mark.parametrize(('extra_user_params', 'isolated'), [
    ({'isolated': True}, True),
    ({'isolated': False}, False),
    ({}, False),
])
def test_is_isolated_build(workflow, extra_user_params, isolated, user_params):
    workflow.user_params.update(extra_user_params)
    assert is_isolated_build(workflow) == isolated


@pytest.mark.parametrize(('extra_user_params', 'flatpak'), [
    ({'flatpak': True}, True),
    ({'flatpak': False}, False),
    ({}, False),
])
def test_is_flatpak_build(workflow, extra_user_params, flatpak, user_params):
    workflow.user_params.update(extra_user_params)
    assert is_flatpak_build(workflow) == flatpak


@pytest.mark.parametrize("is_orchestrator", [True, False])
def test_get_orchestrator_platforms(is_orchestrator, workflow):
    env = MockEnv(workflow).set_user_params(platforms=["x86_64", "ppc64le"])
    if is_orchestrator:
        env.make_orchestrator()

    if is_orchestrator:
        assert get_orchestrator_platforms(env.workflow) == ["x86_64", "ppc64le"]
    else:
        assert get_orchestrator_platforms(env.workflow) is None


def test_df_parser(tmpdir):
    tmpdir_path = str(tmpdir.realpath())
    df = df_parser(tmpdir_path)
    df.lines = [
        "FROM fedora\n",
        "ENV foo=\"bar\"\n",
        "LABEL label=\"foobar barfoo\"\n"
    ]

    assert len(df.envs) == 1
    assert df.envs.get('foo') == 'bar'
    assert len(df.labels) == 1
    assert df.labels.get('label') == 'foobar barfoo'


def test_df_parser_parent_env_arg(tmpdir):
    p_env = {
        "test_env": "first"
    }
    df_content = dedent("""\
        FROM fedora
        ENV foo=bar
        LABEL label="foobar $test_env"
        """)
    df = df_parser(str(tmpdir), parent_env=p_env)
    df.content = df_content
    assert df.labels.get('label') == 'foobar first'


@pytest.mark.parametrize('env_arg', [
    {"test_env": "first"},
    ['test_env=first'],
    ['test_env='],
    ['test_env=--option=first --option=second'],
    ['test_env_first'],
])
def test_df_parser_parent_env_wf(tmpdir, workflow, caplog, env_arg):
    df_content = dedent("""\
        FROM fedora
        ENV foo=bar
        LABEL label="foobar $test_env"
        """)
    env_conf = {INSPECT_CONFIG: {"Env": env_arg}}
    workflow.source = StubSource()
    flexmock(workflow.imageutil).should_receive('base_image_inspect').and_return(env_conf)
    df = df_parser(str(tmpdir), workflow=workflow)
    df.content = df_content

    if isinstance(env_arg, list) and ('=' not in env_arg[0]):
        expected_log_message = "Unable to parse all of Parent Config ENV"
        assert expected_log_message in [log.getMessage() for log in caplog.records]
    elif isinstance(env_arg, dict):
        assert df.labels.get('label') == ('foobar ' + env_arg['test_env'])
    else:
        assert df.labels.get('label') == 'foobar ' + env_arg[0].split('=', 1)[1]


@pytest.mark.parametrize(('available', 'requested', 'result'), (
    (['spam', 'bacon', 'eggs'], ['spam'], True),
    (['spam', 'bacon', 'eggs'], ['spam', 'bacon'], True),
    (['spam', 'bacon', 'eggs'], ['spam', 'bacon', 'eggs'], True),
    (['spam', 'bacon', 'eggs'], ['spam', 'eggs'], True),
    (['spam', 'bacon', 'eggs'], ['eggs', 'spam'], False),
    (['spam', 'bacon', 'eggs'], ['spam', 'eggs', 'bacon'], False),
    (['spam', 'bacon', 'eggs'], ['sausage'], False),
))
def test_are_plugins_in_order(available, requested, result):
    assert are_plugins_in_order([{'name': plugin} for plugin in available],
                                *requested) == result


@pytest.mark.parametrize(('test_string', 'labels', 'expected'), [
    ('', {}, ''),
    ('', {'version': 'cat'}, ''),
    ('dog', {'version': 'cat'}, 'dog'),
    ('dog', {}, 'dog'),
    ('{version}', {'version': 'cat'}, 'cat'),
    ('dog-{version}', {'version': 'cat'}, 'dog-cat'),
    ('{version}', {}, None),
    ('{Version}', {'version': 'cat'}, None),
])
def test_label_formatter(labels, test_string, expected):
    if expected is not None:
        assert expected == LabelFormatter().vformat(test_string, [], labels)
    else:
        with pytest.raises(KeyError):
            LabelFormatter().vformat(test_string, [], labels)


@pytest.mark.parametrize(('key', 'value', 'expected'), [
    ('a', 'b', '"a"="b"'),
    ('a"', 'b"', '"a\\""="b\\""'),
    ('a""', 'b""', '"a\\"\\""="b\\"\\""'),
    ('a\\', 'b\\', '"a\\\\"="b\\\\"'),
])
def test_label_to_string(key, value, expected):
    assert expected == label_to_string(key, value)


@pytest.mark.parametrize(('tag_conf', 'expected_primary',
                          'expected_floating', 'expected_unique'), (
    (['spam', 'bacon'], [], ['spam', 'bacon'], []),
    (['spam-bacon'], ['spam-bacon'], [], []),
    ([], [], [], []),
    (['spam_unique'], [], [], ['spam_unique']),
    (['spam', 'bacon-toast', 'bacon_unique'], ['bacon-toast'], ['spam'], ['bacon_unique']),
))
def test_get_primary_and_floating_images(workflow, tag_conf, expected_primary,
                                         expected_floating, expected_unique):
    template_image = ImageName.parse('registry.example.com/fedora')

    wf_data = workflow.data
    for tag in tag_conf:
        image_name = ImageName.parse(str(template_image))
        image_name.tag = tag
        if '-' in tag:
            wf_data.tag_conf.add_primary_image(str(image_name))
        elif 'unique' in tag:
            wf_data.tag_conf.add_unique_image(str(image_name))
        else:
            wf_data.tag_conf.add_floating_image(str(image_name))

    build_result = BuildResult(image_id='foo')
    workflow.data.build_result = build_result

    actual_primary = get_primary_images(workflow)
    actual_floating = get_floating_images(workflow)
    actual_unique = get_unique_images(workflow)
    assert len(actual_primary) == len(expected_primary)
    assert len(actual_floating) == len(expected_floating)
    assert len(actual_unique) == len(expected_unique)

    for index, primary_image in enumerate(actual_primary):
        assert primary_image.registry == template_image.registry
        assert primary_image.namespace == template_image.namespace
        assert primary_image.repo == template_image.repo

        assert primary_image.tag == expected_primary[index]

    for index, floating_image in enumerate(actual_floating):
        assert floating_image.registry == template_image.registry
        assert floating_image.namespace == template_image.namespace
        assert floating_image.repo == template_image.repo

        assert floating_image.tag == expected_floating[index]

    for index, unique_image in enumerate(actual_unique):
        assert unique_image.registry == template_image.registry
        assert unique_image.namespace == template_image.namespace
        assert unique_image.repo == template_image.repo

        assert unique_image.tag == expected_unique[index]


@pytest.mark.parametrize('source', ['file', 'string', 'url'])
@pytest.mark.parametrize('config', [
    ("""\
      version: 1
      koji:
        hub_url: /
        root_url: ''
        auth: {}
      openshift:
        url: openshift_url
      registries:
        - url: registry
      source_registry:
        url: source_registry
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
    """),
    REACTOR_CONFIG_MAP,
])
@responses.activate
def test_read_yaml(tmpdir, source, config):
    expected = yaml.safe_load(config)

    if source == 'file':
        config_path = os.path.join(str(tmpdir), 'config.yaml')
        with open(config_path, 'w') as fp:
            fp.write(config)
        output = read_yaml_from_file_path(config_path, 'schemas/config.json')
    elif source == 'url':
        url = 'https://somewhere.net/config.yaml'
        responses.add(responses.GET, url, body=config)
        output = read_yaml_from_url(url, 'schemas/config.json')
    else:
        output = read_yaml(config, 'schemas/config.json')

    assert output == expected


@pytest.mark.parametrize('content_type', ['application/octet-stream', 'text/plain'])
@responses.activate
def test_read_yaml_from_url_different_content_types(content_type):
    url = 'https://somewhere.net/config.yaml'
    responses.add(responses.GET, url, body=REACTOR_CONFIG_MAP, content_type=content_type)
    output = read_yaml_from_url(url, 'schemas/config.json')
    expected = yaml.safe_load(REACTOR_CONFIG_MAP)
    assert output == expected


@pytest.mark.parametrize(
    "data, schema, valid",
    [
        (yaml.safe_load(REACTOR_CONFIG_MAP), "schemas/config.json", True),
        ({"exit_plugins": [{"name": None}]}, "schemas/plugins.json", False),
    ],
)
def test_validate_with_schema(data, schema, valid):
    if valid:
        validate_with_schema(data, schema)
    else:
        with pytest.raises(OsbsValidationException):
            validate_with_schema(data, schema)


LogEntry = namedtuple('LogEntry', ['platform', 'line'])


def test_osbs_logs_get_log_files(tmpdir):
    class OSBS(object):
        def get_build_logs(self, pipeline_run_name):
            logs = {
                "taskRun1": {"containerA": "log message A", "containerB": "log message B"},
                "taskRun2": {"containerC": "log message C"},
            }
            return logs

    osbs_logfile_metadata = {
        'checksum': '1b6c0f6e47915b0d0d12cc0fc863750a',
        'checksum_type': 'md5',
        'filename': OSBS_BUILD_LOG_FILENAME,
        'filesize': 42
    }

    logger = flexmock()
    flexmock(logger).should_receive('error')
    osbs_logs = OSBSLogs(logger)
    osbs = OSBS()
    output = osbs_logs.get_log_files(osbs, 'test-pipeline-run')
    assert output[0].metadata == osbs_logfile_metadata


@pytest.mark.parametrize('raise_error', [
    HTTPError,
    RetryError,
    None,
])
@pytest.mark.parametrize('insecure', [
    True,
    False,
])
@pytest.mark.parametrize('creds', [
    ('user1', 'pass'),
    (None, 'pass'),
    ('user1', None),
    None,
])
@pytest.mark.parametrize('image,registry,path', [
    ('not-used.com/spam:latest', 'localhost.com',
     '/v2/spam/manifests/latest'),

    ('not-used.com/food/spam:latest', 'http://localhost.com',
     '/v2/food/spam/manifests/latest'),

    ('not-used.com/spam', 'https://localhost.com',
     '/v2/spam/manifests/latest'),
])
@responses.activate
def test_get_manifest_list(tmpdir, raise_error, image, registry, insecure, creds, path):
    kwargs = {}

    image = ImageName.parse(image)
    kwargs['image'] = image

    if creds:
        temp_dir = mkdtemp(dir=str(tmpdir))
        with open(os.path.join(temp_dir, '.dockercfg'), 'w+') as dockerconfig:
            dockerconfig.write(json.dumps({
                registry: {
                    'username': creds[0], 'password': creds[1]
                }
            }))
        kwargs['dockercfg_path'] = temp_dir

    kwargs['registry'] = registry

    if insecure is not None:
        kwargs['insecure'] = insecure

    def request_callback(request, all_headers=True):
        if creds and creds[0] and creds[1]:
            assert request.headers['Authorization']

        if raise_error:
            raise raise_error('request test error')

        media_type = request.headers['Accept']
        if media_type.endswith('list.v2+json'):
            digest = 'v2_list-digest'
        elif media_type.endswith('v2+json'):
            digest = 'v2-digest'
        elif media_type.endswith('v1+json'):
            digest = 'v1-digest'
        else:
            raise ValueError('Unexpected media type {}'.format(media_type))

        media_type_prefix = media_type.split('+')[0]
        if all_headers:
            headers = {
                'Content-Type': '{}+jsonish'.format(media_type_prefix),
            }
            if not media_type.endswith('list.v2+json'):
                headers['Docker-Content-Digest'] = digest
        else:
            headers = {}
        return (200, headers, '')

    if registry.startswith('http'):
        url = registry + path
    else:
        # In the insecure case, we should try the https URL, and when that produces
        # an error, fall back to http
        if insecure:
            https_url = 'https://' + registry + path
            responses.add(responses.GET, https_url, body=requests.ConnectionError())
            url = 'http://' + registry + path
        else:
            url = 'https://' + registry + path
    responses.add_callback(responses.GET, url, callback=request_callback)

    if raise_error:
        with pytest.raises(raise_error) as e:
            get_manifest_list(**kwargs)
        assert 'request test error' in str(e.value)
        return

    manifest_list = get_manifest_list(**kwargs)
    assert manifest_list


@pytest.mark.parametrize('insecure', [
    True,
    False,
])
@pytest.mark.parametrize('creds', [
    ('user1', 'pass'),
    (None, 'pass'),
    ('user1', None),
    None,
])
@pytest.mark.parametrize('image,registry,path', [
    ('not-used.com/spam:latest', 'localhost.com',
     '/v2/spam/manifests/latest'),

    ('not-used.com/food/spam:latest', 'http://localhost.com',
     '/v2/food/spam/manifests/latest'),

    ('not-used.com/spam', 'https://localhost.com',
     '/v2/spam/manifests/latest'),
])
@pytest.mark.parametrize('versions', [
    ('v1', 'v2', 'v2_list'),
    ('v1', 'v2'),
    ('v1', 'v2_list'),
    ('v2', 'v2_list'),
    ('v1',),
    ('v1',),
    ('v2_list',),
    tuple(),
    None,
])
@responses.activate
def test_get_all_manifests(tmpdir, image, registry, insecure, creds, path, versions):
    kwargs = {}

    image = ImageName.parse(image)
    kwargs['image'] = image

    if creds:
        temp_dir = mkdtemp(dir=str(tmpdir))
        with open(os.path.join(temp_dir, '.dockercfg'), 'w+') as dockerconfig:
            dockerconfig.write(json.dumps({
                registry: {
                    'username': creds[0], 'password': creds[1]
                }
            }))
        kwargs['dockercfg_path'] = temp_dir

    kwargs['registry'] = registry

    if insecure is not None:
        kwargs['insecure'] = insecure

    if versions is not None:
        kwargs['versions'] = versions
    expected_versions = versions
    if versions is None:
        # Test default versions value
        expected_versions = ('v1', 'v2', 'v2_list')

    def request_callback(request, all_headers=True):
        if creds and creds[0] and creds[1]:
            assert request.headers['Authorization']

        media_type = request.headers['Accept']
        if media_type.endswith('list.v2+json'):
            digest = 'v2_list-digest'
        elif media_type.endswith('v2+json'):
            digest = 'v2-digest'
        elif media_type.endswith('v1+json'):
            digest = 'v1-digest'
        else:
            raise ValueError('Unexpected media type {}'.format(media_type))

        media_type_prefix = media_type.split('+')[0]
        if all_headers:
            headers = {
                'Content-Type': '{}+jsonish'.format(media_type_prefix),
            }
            if not media_type.endswith('list.v2+json'):
                headers['Docker-Content-Digest'] = digest
        else:
            headers = {}
        return (200, headers, '')

    if registry.startswith('http'):
        url = registry + path
    else:
        # In the insecure case, we should try the https URL, and when that produces
        # an error, fall back to http
        if insecure:
            https_url = 'https://' + registry + path
            responses.add(responses.GET, https_url, body=requests.ConnectionError())
            url = 'http://' + registry + path
        else:
            url = 'https://' + registry + path
    responses.add_callback(responses.GET, url, callback=request_callback)

    all_manifests = get_all_manifests(**kwargs)
    if expected_versions:
        assert all_manifests
        for version in expected_versions:
            assert version in all_manifests
    else:
        assert all_manifests == {}


@pytest.mark.parametrize(('valid'), [
    True,
    False
])
@pytest.mark.parametrize(('platforms', 'config_dict', 'result'), [
    (
        ['x86_64', 'ppc64le'],
        {'platforms': {'only': 'ppc64le'}},
        ['ppc64le']
    ), (
        ['x86_64', 'spam', 'bacon', 'toast', 'ppc64le'],
        {'platforms': {'not': ['spam', 'bacon', 'eggs', 'toast']}},
        ['x86_64', 'ppc64le']
    ), (
        ['ppc64le', 'spam', 'bacon', 'toast'],
        {'platforms': {'not': ['spam', 'bacon', 'eggs', 'toast'], 'only': ['ppc64le']}},
        ['ppc64le']
    ), (
        ['x86_64', 'bacon', 'toast'],
        {'platforms': {'not': 'toast', 'only': ['x86_64', 'ppc64le']}},
        ['x86_64']
    ), (
        ['x86_64', 'toast'],
        {'platforms': {'not': 'toast', 'only': 'x86_64'}},
        ['x86_64']
    ), (
        ['x86_64', 'spam', 'bacon', 'toast'],
        {'platforms': {
            'not': ['spam', 'bacon', 'eggs', 'toast'],
            'only': ['x86_64', 'ppc64le']
        }},
        ['x86_64']
    ), (
        ['x86_64', 'ppc64le'],
        {},
        ['x86_64', 'ppc64le']
    ), (
        ['x86_64', 'ppc64le'],
        {'platforms': {'not': 'x86_64', 'only': 'x86_64'}},
        []
    ), (
        ['x86_64', 'ppc64le'],
        {'platforms': None},
        ['x86_64', 'ppc64le']
    ),
])
def test_get_platforms_in_limits(tmpdir, platforms, config_dict, result, valid, caplog):
    class MockSource(object):
        def __init__(self, build_dir):
            self.build_dir = build_dir
            self._config = None

        def get_build_file_path(self):
            return self.build_dir, self.build_dir

        @property
        def config(self):
            self._config = self._config or SourceConfig(self.build_dir)
            return self._config

    class MockWorkflow(object):
        def __init__(self, build_dir):
            self.source = MockSource(build_dir)

    def configured_same_not_and_only(conf):

        def to_set(conf):
            return set([conf] if not isinstance(conf, list) else conf)

        platforms_conf = conf.get('platforms', {})
        if not platforms_conf:
            return False

        excluded_platforms_conf = platforms_conf.get('not', [])
        excluded_platforms = to_set(excluded_platforms_conf)
        if not excluded_platforms:
            return False

        only_platforms_conf = platforms_conf.get('only', [])
        only_platforms = to_set(only_platforms_conf)
        return excluded_platforms == only_platforms

    with open(os.path.join(str(tmpdir), 'container.yaml'), 'w') as f:
        f.write(yaml.safe_dump(config_dict))
        f.flush()
    if valid and platforms:
        workflow = MockWorkflow(str(tmpdir))
        final_platforms = get_platforms_in_limits(workflow, platforms)
        if configured_same_not_and_only(config_dict):
            assert 'only and not platforms are the same' in caplog.text
        assert final_platforms == set(result)
    elif valid:
        workflow = MockWorkflow(str(tmpdir))
        final_platforms = get_platforms_in_limits(workflow, platforms)
        assert final_platforms is None
    else:
        workflow = MockWorkflow('bad_dir')
        final_platforms = get_platforms_in_limits(workflow, platforms)
        assert final_platforms == set(platforms)


MOCK_INSPECT_DATA = {
    'created': 'create_time',
    'os': 'os version',
    'container_config': 'container config',
    'architecture': 'arch',
    'docker_version': 'docker version',
    'config': 'conf',
    'rootfs': {'type': 'layers', 'diff_ids': ['sha256:123456', 'sha256:abcdef']}
}
MOCK_CONFIG_DIGEST = '987654321'

MOCK_EXPECT_INSPECT = {
    'Created': 'create_time',
    'Os': 'os version',
    'ContainerConfig': 'container config',
    'Architecture': 'arch',
    'DockerVersion': 'docker version',
    'Config': 'conf',
    'RootFS': {'Type': 'layers', 'Layers': ['sha256:123456', 'sha256:abcdef']},
    'Id': MOCK_CONFIG_DIGEST
}


@pytest.mark.parametrize('insecure', [True, False])
@pytest.mark.parametrize(('found_versions', 'type_in_list', 'will_raise'), [
    (('v1', 'v2', 'v2_list'), MEDIA_TYPE_DOCKER_V2_SCHEMA1, True),
    (('v1', 'v2', 'v2_list'), MEDIA_TYPE_DOCKER_V2_SCHEMA2, False),
    (('v1', 'v2'), None, False),
    (('v1', 'v2_list'), MEDIA_TYPE_DOCKER_V2_SCHEMA1, True),
    (('v1', 'v2_list'), MEDIA_TYPE_DOCKER_V2_SCHEMA2, False),
    (('v2', 'v2_list'), MEDIA_TYPE_DOCKER_V2_SCHEMA1, True),
    (('v2', 'v2_list'), MEDIA_TYPE_DOCKER_V2_SCHEMA2, False),
    (('v1',), None, False),
    (('v2',), None, False),
    (('v2_list',), MEDIA_TYPE_DOCKER_V2_SCHEMA1, True),
    (('v2_list',), MEDIA_TYPE_DOCKER_V2_SCHEMA2, False),
    (tuple(), None, True),
])
def test_get_inspect_for_image(insecure, found_versions, type_in_list, will_raise):
    image_with_reg = 'localhost.com/not-used.com/spam:latest'
    image = ImageName.parse(image_with_reg)

    if not found_versions:
        raise_exception = RuntimeError
        error_msg = (
            'Image {image_name} not found: No v2 schema 1 image, '
            'or v2 schema 2 image or list, found'.format(image_name=image)
        )
    elif 'v2_list' in found_versions and will_raise:
        raise_exception = RuntimeError
        error_msg = 'Image {image_name}: v2 schema 1 in manifest list'.format(image_name=image)

    inspect_data = MOCK_INSPECT_DATA.copy()
    config_digest = MOCK_CONFIG_DIGEST
    expect_inspect = MOCK_EXPECT_INSPECT.copy()

    if found_versions == ('v1', ):
        config_digest = None
        expect_inspect.pop('RootFS')
        expect_inspect['Id'] = None
        inspect_data.pop('rootfs')

    v2_list_json = {'manifests': [{'mediaType': type_in_list, 'digest': 12345}]}
    v2_list_response = flexmock(json=lambda: v2_list_json, status_code=200)

    v1_json = {'history': [{'v1Compatibility': json.dumps(inspect_data)}]}
    v1_response = flexmock(json=lambda: v1_json, status_code=200)

    v2_json = {'config': {'digest': config_digest}}
    v2_response = flexmock(json=lambda: v2_json, status_code=200)

    return_list = {}
    for version in found_versions:
        if version == 'v1':
            return_list[version] = v1_response
        elif version == 'v2':
            return_list[version] = v2_response
        elif version == 'v2_list':
            return_list[version] = v2_list_response

    (flexmock(atomic_reactor.util.RegistryClient)
     .should_receive('get_all_manifests')
     .and_return(return_list)
     .once())

    if will_raise:
        with pytest.raises(raise_exception) as e:
            get_inspect_for_image(image, image.registry, insecure)
        assert error_msg in str(e.value)

    else:
        if 'v2_list' in found_versions:
            (flexmock(atomic_reactor.util.RegistryClient)
             .should_receive('get_config_and_id_from_registry')
             .and_return(inspect_data, config_digest)
             .once())
        elif 'v2' in found_versions:
            (flexmock(atomic_reactor.util.RegistryClient)
             .should_receive('_blob_config_by_digest')
             .with_args(image, config_digest)
             .and_return(inspect_data)
             .once())

        inspected = get_inspect_for_image(image, image.registry, insecure)
        assert inspected == expect_inspect


@pytest.mark.parametrize("n_matches", [0, 1, 2])
def test_get_inspect_for_image_specific_arch(n_matches, caplog):
    """Test that choosing the specified architecture from a manifest list works as expected."""
    armv6_v2_digest = "123456"
    armv7_v2_digest = "987654"
    s390x_v2_digest = "abcdef"

    def manifest_in_list(arch, digest, variant=None):
        manifest = {
            "mediaType": MEDIA_TYPE_DOCKER_V2_SCHEMA2,
            "digest": digest,
            "platform": {"architecture": arch},
        }
        if variant:
            manifest["platform"]["variant"] = variant
        return manifest

    all_manifests = {
        "v2_list": flexmock(
            json=lambda: {
                "manifests": [
                    manifest_in_list("arm", armv6_v2_digest, variant="v6"),
                    manifest_in_list("arm", armv7_v2_digest, variant="v7"),
                    manifest_in_list("s390x", s390x_v2_digest),
                ],
            },
            status_code=200,
        ),
    }
    (flexmock(atomic_reactor.util.RegistryClient)
     .should_receive("get_all_manifests")
     .and_return(all_manifests)
     .once())

    image = ImageName.parse("example.org/foo/bar:latest")

    if n_matches == 1:
        want_arch = "s390x"
        expect_log = None
        expect_error_message = None
    elif n_matches == 0:
        want_arch = "ppc64le"
        expect_log = "Expected one ppc64le manifest in manifest list, got []"
        expect_error_message = (
            "Expected exactly one manifest for ppc64le architecture in manifest list, got 0"
        )
    elif n_matches == 2:
        want_arch = "arm"
        arm_manifests = [
            manifest_in_list("arm", armv6_v2_digest, variant="v6"),
            manifest_in_list("arm", armv7_v2_digest, variant="v7"),
        ]
        expect_log = f"Expected one arm manifest in manifest list, got {arm_manifests}"
        expect_error_message = (
            "Expected exactly one manifest for arm architecture in manifest list, got 2"
        )
    else:
        assert False, f"the test doesn't work for {n_matches} matches"

    if expect_error_message:
        with pytest.raises(RuntimeError, match=expect_error_message):
            get_inspect_for_image(image, image.registry, arch=want_arch)

        assert expect_log in caplog.text
        return

    # else (success):
    inspect_data = {**MOCK_INSPECT_DATA, "architecture": want_arch}
    config_digest = MOCK_CONFIG_DIGEST
    expect_inspect = {**MOCK_EXPECT_INSPECT, "Architecture": want_arch}

    (flexmock(atomic_reactor.util.RegistryClient)
     .should_receive("get_config_and_id_from_registry")
     # this is the important part - config must be queried by the s390x digest
     .with_args(image, s390x_v2_digest, version="v2")
     .and_return(inspect_data, config_digest)
     .once())

    assert get_inspect_for_image(image, image.registry, arch=want_arch) == expect_inspect


def test_get_inspect_for_image_empty_manifest_list(caplog):
    all_manifests = {
        "v2_list": flexmock(
            json=lambda: {"manifests": []},
            status_code=200,
        ),
    }
    (flexmock(atomic_reactor.util.RegistryClient)
     .should_receive("get_all_manifests")
     .and_return(all_manifests)
     .once())

    image = ImageName.parse("example.org/foo/bar:latest")

    with pytest.raises(RuntimeError, match="Manifest list is empty"):
        get_inspect_for_image(image, image.registry)

    assert f"Empty manifest list: {all_manifests['v2_list'].json()}" in caplog.text


def test_get_inspect_for_image_wrong_arch():
    """Test that an error is raised when inspecting a non-list image for the wrong architecture."""
    all_manifests = {
        "v2": flexmock(
            json=lambda: {"config": {"digest": MOCK_CONFIG_DIGEST}},
            status_code=200,
        ),
    }
    (flexmock(atomic_reactor.util.RegistryClient)
     .should_receive("get_all_manifests")
     .and_return(all_manifests)
     .once())

    image = ImageName.parse("example.org/foo/bar:latest")
    inspect_data = {**MOCK_INSPECT_DATA, "architecture": "amd64"}

    (flexmock(atomic_reactor.util.RegistryClient)
     .should_receive('_blob_config_by_digest')
     .and_return(inspect_data)
     .once())

    with pytest.raises(
        RuntimeError,
        match="Has architecture amd64, which does not match specified architecture s390x",
    ):
        get_inspect_for_image(image, image.registry, arch="s390x")


def test_dump_stacktraces(capfd):
    log_msg = '(most recent call first)'
    func_name = inspect.currentframe().f_code.co_name
    _, err = capfd.readouterr()
    assert log_msg not in err
    assert func_name not in err
    dump_stacktraces(signal.SIGUSR1, inspect.currentframe())
    _, err = capfd.readouterr()
    assert log_msg in err
    assert func_name in err


def test_introspection_signal_handler(capfd):
    log_msg = '(most recent call first)'
    _, err = capfd.readouterr()
    assert log_msg not in err
    pid = os.getpid()
    sig = signal.SIGUSR1
    setup_introspection_signal_handler()
    os.kill(pid, sig)
    _, err = capfd.readouterr()
    assert log_msg in err


@pytest.mark.parametrize('dockerignore_exists', [True, False])
def test_allow_repo_dir_in_dockerignore(tmpdir, dockerignore_exists):
    docker_ignore_file = os.path.join(str(tmpdir), DOCKERIGNORE)

    ignore_content = ["# Ignore everything so we can just have a whitelist of things to copy\n",
                      "** / *\n"]
    added_lines = "!%s\n" % RELATIVE_REPOS_PATH

    if dockerignore_exists:
        with open(docker_ignore_file, "w") as f:
            for line in ignore_content:
                f.write(line)

    allow_repo_dir_in_dockerignore(tmpdir)

    if dockerignore_exists:
        with open(docker_ignore_file, "r") as f:
            ignore_lines = f.readlines()

        assert ignore_lines[0:len(ignore_content)] == ignore_content
        assert ignore_lines[-1] == added_lines


@pytest.mark.parametrize('labels,f_true,f_false', [
    (
        ['com.redhat.delivery.appregistry=true'],
        [has_operator_appregistry_manifest],
        [has_operator_bundle_manifest],
    ),
    (
        ['com.redhat.delivery.operator.bundle=true'],
        [has_operator_bundle_manifest],
        [has_operator_appregistry_manifest],
    ),
    (
        ['com.redhat.delivery.operator.bundle=true',
         'com.redhat.delivery.appregistry=true'],
        [has_operator_bundle_manifest, has_operator_appregistry_manifest],
        [],
    ),
    (
        ['com.redhat.delivery.operator.bundle=meh',
         'com.redhat.delivery.appregistry=meh'],
        [],
        [has_operator_bundle_manifest, has_operator_appregistry_manifest],
    )

])
def test_has_operator_manifest(workflow, labels, f_true, f_false):
    df_content = dedent("""\
        FROM fedora
        ENV foo=bar
        """)
    for label in labels:
        df_content += 'LABEL {}\n'.format(label)

    workflow.build_dir.init_build_dirs(["x86_64"], workflow.source)
    workflow.build_dir.any_platform.dockerfile.content = df_content

    for func in f_true:
        assert func(workflow), 'Label not properly detected'

    for func in f_false:
        assert not func(workflow), 'Label false positively detected'


class TestRegistryClient(object):
    """Tests for RegistryClient class"""

    @responses.activate
    def test_get_manifest_list_digest(self):
        registry_url = 'https://reg.test'

        def mock_digest_query(image):
            i = image
            url = '{}/v2/{}/{}/manifests/{}'.format(registry_url, i.namespace, i.repo, i.tag)
            headers = {
                'Content-Type': MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST,
            }
            manifest_list_json = (
                '{"schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.'
                'list.v2+json", "manifests": [{"mediaType": "application/vnd.docker.distribution.'
                'manifest.v2+json", "size": 429, "digest": "sha256:e9aacf364fd8b2912c6fa94d55f723d'
                '3b9d03c0b4748798ad35792f6629b5cd3", "platform": {"architecture": "amd64", "os": "'
                'linux"}}, {"mediaType": "application/vnd.docker.distribution.manifest.v2+json", "'
                'size": 429, "digest": "sha256:e445f6ec6343730b5062284442643376d861ceb8965e3c67647'
                '65bba70251a88", "platform": {"architecture": "arm64", "os": "linux"}}, {"mediaTyp'
                'e": "application/vnd.docker.distribution.manifest.v2+json", "size": 429, "digest"'
                ': "sha256:9de6c900589f6ff2f4273a0e15825bcee91fbc291e1ad532b2e1796bd1519393", "pla'
                'tform": {"architecture": "ppc64le", "os": "linux"}}, {"mediaType": "application/v'
                'nd.docker.distribution.manifest.v2+json", "size": 429, "digest": "sha256:27bc6ef6'
                '3b9f8d24632f9b74985f254ac3d45b28ed315bcd7babf4f512db1187", "platform": {"architec'
                'ture": "s390x", "os": "linux"}}]}'
            )
            responses.add(responses.GET, url, headers=headers, body=manifest_list_json)

        session = RegistrySession(registry_url)
        client = atomic_reactor.util.RegistryClient(session)
        image = ImageName.parse("namespace/fedora:32")
        mock_digest_query(image)
        expected = "sha256:d84ad27a3055f11cf2d34e611b8d14aada444e1e71866ea6a076b773aeac3c93"
        assert client.get_manifest_list_digest(image) == expected


@pytest.mark.parametrize(('source_registry', 'organization'), [
    (None, None),
    ('source_registry.com', None),
    ('source_registry.com', 'organization'),
])
@pytest.mark.parametrize(('dockerfile_images', 'base_from_scratch', 'custom_base_image',
                          'custom_parent_image', 'bool_val', 'length'), [
    ([],
     False, False, False, False, 0),
    (['scratch'],
     True, False, False, False, 0),
    (['koji/image-build'],
     False, True, True, True, 1),
    (['parent_image:latest', 'koji/image-build'],
     False, True, True, True, 2),
    (['koji/image-build', 'base_image:latest'],
     False, False, True, True, 2),
    (['parent_image:latest', 'scratch'],
     True, False, False, True, 1),
    (['scratch', 'base_image:latest'],
     False, False, False, True, 1),
    (['parent_image:latest', 'base_image:latest'],
     False, False, False, True, 2),
    (['different_registry.com/parent_image:latest', 'base_image:latest'],
     False, False, False, True, 2),
    (['parent_image:latest', 'different_registry.com/base_image:latest'],
     False, False, False, True, 2),
    (['different_registry.com/parent_image:latest', 'different_registry.com/base_image:latest'],
     False, False, False, True, 2),
])
def test_dockerfile_images(source_registry, organization, dockerfile_images, base_from_scratch,
                           custom_base_image, custom_parent_image, bool_val, length):
    df_image = DockerfileImages(dockerfile_images)

    # bool value of dockerfile_images
    if df_image:
        assert bool_val is True
    else:
        assert bool_val is False

    # check that all images exist
    for img in dockerfile_images:
        if img != 'scratch':
            df_image[img]  # pylint: disable=pointless-statement

    assert df_image.original_parents == dockerfile_images
    assert df_image.original_base_image == (dockerfile_images[-1] if dockerfile_images else None)
    assert df_image.base_from_scratch == base_from_scratch
    assert df_image.custom_base_image == custom_base_image
    assert df_image.custom_parent_image == custom_parent_image
    assert len(df_image) == length

    expect_keys = [ImageName.parse(img) for img in reversed(dockerfile_images) if img != 'scratch']
    assert expect_keys == df_image.keys()

    base_image_key = None
    if dockerfile_images:
        if dockerfile_images[-1] == 'scratch':
            base_image_key = 'scratch'
        else:
            base_image_key = ImageName.parse(dockerfile_images[-1])

    if not dockerfile_images:
        with pytest.raises(KeyError):
            df_image.base_image_key  # pylint: disable=pointless-statement
            df_image.base_image  # pylint: disable=pointless-statement
    else:
        assert df_image.base_image_key == base_image_key
        assert df_image.base_image == base_image_key

    if source_registry:
        # set source registry and org and do checks again
        df_image.set_source_registry(source_registry, organization)

        assert df_image.source_registry == source_registry
        assert df_image.organization == organization

        for img in dockerfile_images:
            if img != 'scratch':
                df_image[img]  # pylint: disable=pointless-statement

        assert df_image.original_parents == dockerfile_images
        assert (df_image.original_base_image ==
                (dockerfile_images[-1] if dockerfile_images else None))

        if (base_image_key and base_image_key != 'scratch' and
                not base_image_is_custom(base_image_key.to_str())):
            if not base_image_key.registry:
                base_image_key.registry = source_registry
                if organization:
                    base_image_key.enclose(organization)
            elif base_image_key.registry == source_registry and organization:
                base_image_key.enclose(organization)

        if not dockerfile_images:
            with pytest.raises(KeyError):
                df_image.base_image_key  # pylint: disable=pointless-statement
                df_image.base_image  # pylint: disable=pointless-statement
        else:
            assert df_image.base_image_key == base_image_key
            assert df_image.base_image == base_image_key

        expect_keys_enclosed = []
        for img in expect_keys:
            if base_image_is_custom(img.to_str()):
                expect_keys_enclosed.append(img)
                continue

            if not img.registry:
                img.registry = source_registry
                if organization:
                    img.enclose(organization)
            elif img.registry == source_registry and organization:
                img.enclose(organization)

            expect_keys_enclosed.append(img)

        assert expect_keys_enclosed == df_image.keys()

        with pytest.raises(RuntimeError):
            df_image.set_source_registry('different_source_registry.com', organization)

    if dockerfile_images:
        # set local tag for all images
        for img in dockerfile_images:
            if img == 'scratch':
                continue

            df_image[img] = 'local:tag'
        local_tag = ImageName.parse('local:tag')

        # check that all images have local tag set
        for img, img_tag in df_image.items():
            assert img_tag == local_tag  # pylint: disable=pointless-statement

        if not df_image.base_from_scratch:
            assert df_image.base_image == local_tag

    # setting non-existing image
    with pytest.raises(KeyError):
        df_image['non-existing'] = 'test'


def test_dockerfile_images_dump_empty_object():
    df_images = DockerfileImages()
    expected = {
        "original_parents": [],
        "source_registry": None,
        "organization": None,
        "local_parents": [],
    }
    assert expected == df_images.as_dict()


@pytest.mark.parametrize(
    "df_parents,expected_local_parents",
    [
        [["scratch"], []],
        [["registry/f:34"], [None]],
    ],
)
def test_dockerfile_images_dump_with_images(
    df_parents: List[str], expected_local_parents
):
    df_images = DockerfileImages(df_parents)
    expected = {
        "original_parents": df_parents,
        "local_parents": expected_local_parents,
        "source_registry": None,
        "organization": None,
    }
    assert expected == df_images.as_dict()


def test_dockerfile_images_load():
    input_data = {
        "original_parents": ["build-base:1.0", "scratch"],
        "local_parents": [None],
        # test the registry is set and pullable images are enclosed properly.
        "source_registry": "registry",
        "organization": "organization",
    }
    df_images = DockerfileImages.load(input_data)

    assert df_images.base_from_scratch
    assert not df_images.custom_base_image
    assert not df_images.custom_parent_image

    assert input_data["original_parents"] == df_images.original_parents
    assert input_data["local_parents"] == df_images._local_parents
    assert input_data["source_registry"] == df_images.source_registry
    assert input_data["organization"] == df_images.organization
    assert df_images._source_and_org_set

    assert len(df_images._pullable_parents) == 1
    pullable_image = ImageName.parse(input_data["original_parents"][0])
    pullable_image.registry = input_data["source_registry"]
    pullable_image.enclose(input_data["organization"])
    assert pullable_image == df_images._pullable_parents[0]


def test_dockerfile_images_dump_and_load():
    """Test the original object can be restored from the dump data."""
    orig_df_images = DockerfileImages(["scratch", "registry/httpd:2.4"])
    loaded_df_images = DockerfileImages.load(orig_df_images.as_dict())
    assert id(orig_df_images) != id(loaded_df_images)
    assert orig_df_images == loaded_df_images


@pytest.mark.parametrize('data,expected', [
    (
        {},
        set()
    ), (
        {'a': 1},
        {('a',)}
    ), (
        {'a': {'b': {'c': {'d1': 1, 'd2': 2}, 'c2': []}}},
        {('a', 'b', 'c2'), ('a', 'b', 'c', 'd1'), ('a', 'b', 'c', 'd2')}
    )
])
def test_terminal_key_paths(data, expected):
    """Unittest for terminal_key_paths data"""
    assert set(terminal_key_paths(data)) == expected


def test_map_to_user_params():
    get_args = map_to_user_params(
        "arg1",
        "arg2:param2",
        "arg_none",
        "arg_missing",
    )
    user_params = {
        "arg1": 1,
        "param2": 2,
        "arg_none": None,
    }
    assert get_args(user_params) == {"arg1": 1, "arg2": 2}


def test_create_tar_gz_archive(tmpdir):
    """Unittest for create_tar_gz_archive method"""

    tmpdir_path = str(tmpdir.realpath())
    test_file = 'foo'
    content = 'bar'
    archive_path = create_tar_gz_archive(file_name=test_file, file_content=content)

    with tarfile.open(archive_path) as tar:

        assert len(tar.getnames()) == 1, 'Tar archive does not contain 1 file'
        assert tar.getnames()[0] == test_file, f'Tar archive does not contain {test_file} file'

        tar.extractall(tmpdir_path)
        extracted_file = os.path.join(tmpdir_path, test_file)

        with open(extracted_file) as f:
            assert f.read() == content, f'Extracted file does not contain {content} string'

    os.remove(extracted_file)
    os.remove(archive_path)
