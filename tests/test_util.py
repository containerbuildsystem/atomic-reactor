"""
Copyright (c) 2015, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import io
import json
import logging
import os
import tempfile
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
import docker
import yaml

from atomic_reactor.build import BuildResult
from atomic_reactor.constants import (IMAGE_TYPE_DOCKER_ARCHIVE, IMAGE_TYPE_OCI, IMAGE_TYPE_OCI_TAR,
                                      MEDIA_TYPE_DOCKER_V2_SCHEMA1, MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                                      DOCKERIGNORE, RELATIVE_REPOS_PATH)
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.util import (wait_for_command,
                                 LazyGit, figure_out_build_file,
                                 render_yum_repo, process_substitutions,
                                 get_checksums, print_version_of_tools,
                                 get_version_of_tools,
                                 human_size, CommandResult,
                                 registry_hostname, Dockercfg, RegistrySession,
                                 get_manifest_digests, ManifestDigest,
                                 get_manifest_list, get_all_manifests,
                                 get_inspect_for_image, get_manifest, get_build_json,
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
                                 OSBSLogs,
                                 get_platforms_in_limits, get_orchestrator_platforms,
                                 dump_stacktraces, setup_introspection_signal_handler,
                                 allow_repo_dir_in_dockerignore,
                                 has_operator_appregistry_manifest,
                                 has_operator_bundle_manifest,
                                 )
from tests.constants import (DOCKERFILE_GIT,
                             INPUT_IMAGE, MOCK, MOCK_SOURCE,
                             REACTOR_CONFIG_MAP)
import atomic_reactor.util
from atomic_reactor.constants import INSPECT_CONFIG, PLUGIN_BUILD_ORCHESTRATE_KEY
from atomic_reactor.source import SourceConfig
from osbs.utils import ImageName
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       ReactorConfig,
                                                       WORKSPACE_CONF_KEY)

from tests.util import requires_internet
from tests.stubs import StubInsideBuilder, StubSource

if MOCK:
    from tests.docker_mock import mock_docker
    from tests.retry_mock import mock_get_retry_session


def test_wait_for_command():
    if MOCK:
        mock_docker()

    d = docker.APIClient()
    logs_gen = d.pull(INPUT_IMAGE, decode=True, stream=True)
    assert wait_for_command(logs_gen) is not None


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


@requires_internet
def test_lazy_git():
    lazy_git = LazyGit(git_url=DOCKERFILE_GIT)
    with lazy_git:
        assert lazy_git.git_path is not None
        assert lazy_git.commit_id is not None
        assert len(lazy_git.commit_id) == 40  # current git hashes are this long

        previous_commit_id = lazy_git.commit_id
        lazy_git.reset('HEAD~2')  # Go back two commits
        assert lazy_git.commit_id is not None
        assert lazy_git.commit_id != previous_commit_id
        assert len(lazy_git.commit_id) == 40  # current git hashes are this long


@requires_internet
def test_lazy_git_with_tmpdir(tmpdir):
    t = str(tmpdir.realpath())
    lazy_git = LazyGit(git_url=DOCKERFILE_GIT, tmpdir=t)
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
    # No pull_registries, have source_registry, should be source_registry
    ('some_registry.io',
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
    reactor_config['version'] = 1   # required key
    workflow.plugin_workspace[ReactorConfigPlugin.key] = {
        WORKSPACE_CONF_KEY: ReactorConfig(reactor_config)
    }

    (flexmock(RegistrySession)
     .should_receive('__init__')
     .with_args(matched_registry['uri'],
                insecure=matched_registry['insecure'],
                dockercfg_path=matched_registry['dockercfg_path'],
                access=access))

    RegistrySession.create_from_config(workflow, registry, access)


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
     'some_registry.io: No match in pull_registries, no source_registry configured'),
    # Registry specified, no source_registry, does not match pull_registries
    ('some_registry.io',
     {
         'pull_registries': [
             {'url': "some_other_registry.io"}
         ]
     },
     'some_registry.io: No match in pull_registries, no source_registry configured'),
])
def test_registry_create_from_config_errors(workflow, registry, reactor_config, error):
    reactor_config['version'] = 1  # required key
    workflow.plugin_workspace[ReactorConfigPlugin.key] = {
        WORKSPACE_CONF_KEY: ReactorConfig(reactor_config)
    }

    with pytest.raises(RuntimeError) as exc_info:
        RegistrySession.create_from_config(workflow, registry)

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


@pytest.mark.parametrize('v1,v2,v2_list,oci,oci_index,default', [
    ('v1-digest', 'v2-digest', None, None, None, 'v2-digest'),
    ('v1-digest', None, None, None, None, 'v1-digest'),
    (None, 'v2-digest', None, None, None, 'v2-digest'),
    (None, 'v2-digest', None, None, None, 'v2-digest'),
    (None, None, None, 'oci-digest', None, 'oci-digest'),
    (None, None, None, None, 'oci-index-digest', 'oci-index-digest'),
    (None, 'v2-digest', None, 'oci-digest', None, 'oci-digest'),
    ('v1-digest', 'v2-digest', 'v2-list-digest', 'oci-digest', 'oci-index-digest',
     'v2-list-digest'),
    (None, 'v2-digest', 'v2-list-digest', 'oci-digest', None, 'v2-list-digest'),
    ('v1-digest', None, 'v2-list-digest', 'oci-digest', None, 'v2-list-digest'),
    ('v1-digest', 'v2-digest', 'v2-list-digest', None, None, 'v2-list-digest'),
    (None, None, None, 'oci-digest', 'oci-index-digest', 'oci-index-digest'),
    (None, None, None, None, None, None),
])
def test_manifest_digest(v1, v2, v2_list, oci, oci_index, default):
    md = ManifestDigest(v1=v1, v2=v2, v2_list=v2_list, oci=oci, oci_index=oci_index)
    assert md.v1 == v1
    assert md.v2 == v2
    assert md.v2_list == v2_list
    assert md.oci == oci
    assert md.default == default
    with pytest.raises(AttributeError):
        assert md.no_such_version


def test_get_manifest_media_version_unknown():
    with pytest.raises(RuntimeError):
        assert get_manifest_media_version(ManifestDigest())


@pytest.mark.parametrize('environ,expected', [
    ({'BUILD': '{"foo": "bar"}'}, {'foo': 'bar'}),
    ({}, False),
])
def test_get_build_json(environ, expected):
    flexmock(os, environ=environ)

    if expected:
        assert get_build_json() == {'foo': 'bar'}
    else:
        with pytest.raises(KeyError):
            get_build_json()


@pytest.mark.parametrize('user_params,scratch', [
    ({'scratch': True}, True),
    ({'scratch': False}, False),
    ({}, False),
])
def test_is_scratch_build(user_params, scratch):
    workflow = DockerBuildWorkflow('test-image', source=MOCK_SOURCE)
    workflow.user_params = user_params
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


@pytest.mark.parametrize(('user_params', 'isolated'), [
    ({'isolated': True}, True),
    ({'isolated': False}, False),
    ({}, False),
])
def test_is_isolated_build(user_params, isolated):
    workflow = DockerBuildWorkflow('test-image', source=MOCK_SOURCE)
    workflow.user_params = user_params
    assert is_isolated_build(workflow) == isolated


@pytest.mark.parametrize(('user_params', 'flatpak'), [
    ({'flatpak': True}, True),
    ({'flatpak': False}, False),
    ({}, False),
])
def test_is_flatpak_build(user_params, flatpak):
    workflow = DockerBuildWorkflow('test-image', source=MOCK_SOURCE)
    workflow.user_params = user_params
    assert is_flatpak_build(workflow) == flatpak


@pytest.mark.parametrize(('inputs', 'results'), [
    (None, None),
    ([], None),
    ([{'name': 'Bogus', 'args': {'platforms': ['spam']}}], None),
    ([{'name': 'Bogus', 'args': {'platforms': ['spam']}},
      {'name': PLUGIN_BUILD_ORCHESTRATE_KEY, 'args': {'platforms': ['ppce64le', 'arm64']}}],
     ['arm64', 'ppce64le'])
])
def test_get_orchestrator_platforms(inputs, results):
    workflow = DockerBuildWorkflow('test-image', source=MOCK_SOURCE,
                                   buildstep_plugins=inputs)
    if results:
        assert sorted(get_orchestrator_platforms(workflow)) == sorted(results)
    else:
        assert get_orchestrator_platforms(workflow) == results


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
    workflow.builder = StubInsideBuilder()
    workflow.builder.set_inspection_data(env_conf)
    df = df_parser(str(tmpdir), workflow=workflow)
    df.content = df_content

    if isinstance(env_arg, list) and ('=' not in env_arg[0]):
        expected_log_message = "Unable to parse all of Parent Config ENV"
        assert expected_log_message in [l.getMessage() for l in caplog.records]
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


@pytest.mark.parametrize(('tag_conf', 'tag_annotation', 'expected_primary',
                          'expected_floating', 'expected_unique'), (
    (['spam', 'bacon'], [], [], ['spam', 'bacon'], []),
    ([], ['spam', 'bacon'], [], ['spam', 'bacon'], []),

    (['spam-bacon'], [], ['spam-bacon'], [], []),
    ([], ['spam-bacon'], ['spam-bacon'], [], []),

    (['spam_unique'], [], [], [], ['spam_unique']),
    ([], ['spam_unique'], [], [], ['spam_unique']),

    (['spam', 'bacon-toast', 'bacon_unique'], [], ['bacon-toast'], ['spam'], ['bacon_unique']),
    ([], ['spam', 'bacon-toast', 'bacon_unique'], ['bacon-toast'], ['spam'], ['bacon_unique']),

    (['spam', 'bacon'], ['ignored', 'scorned'], [], ['spam', 'bacon'], []),
))
def test_get_primary_and_floating_images(workflow, tag_conf, tag_annotation, expected_primary,
                                         expected_floating, expected_unique):
    template_image = ImageName.parse('registry.example.com/fedora')

    for tag in tag_conf:
        image_name = ImageName.parse(str(template_image))
        image_name.tag = tag
        if '-' in tag:
            workflow.tag_conf.add_primary_image(str(image_name))
        elif 'unique' in tag:
            workflow.tag_conf.add_unique_image(str(image_name))
        else:
            workflow.tag_conf.add_floating_image(str(image_name))

    annotations = {'repositories': {'primary': [], 'floating': [], 'unique': []}}
    for tag in tag_annotation:
        image_name = ImageName.parse(str(template_image))
        image_name.tag = tag

        if '-' in tag:
            annotations['repositories']['primary'].append(str(image_name))
        elif 'unique' in tag:
            annotations['repositories']['unique'].append(str(image_name))
        else:
            annotations['repositories']['floating'].append(str(image_name))

    build_result = BuildResult(annotations=annotations, image_id='foo')
    workflow.build_result = build_result

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


LogEntry = namedtuple('LogEntry', ['platform', 'line'])


def test_osbs_logs_get_log_files(tmpdir):
    class OSBS(object):
        def get_orchestrator_build_logs(self, build_id):
            logs = [LogEntry(None, 'orchestrator'),
                    LogEntry('x86_64', 'Hurray for bacon: \u2017'),
                    LogEntry('x86_64', 'line 2')]
            return logs

    metadata = {
        'x86_64.log': {
            'checksum': 'c2487bf0142ea344df8b36990b0186be',
            'checksum_type': 'md5',
            'filename': 'x86_64.log',
            'filesize': 29
        },
        'orchestrator.log': {
            'checksum': 'ac9ed4cc35a9a77264ca3a0fb81be117',
            'checksum_type': 'md5',
            'filename': 'orchestrator.log',
            'filesize': 13
        }
    }

    logger = flexmock()
    flexmock(logger).should_receive('error')
    osbs_logs = OSBSLogs(logger)
    osbs = OSBS()
    output = osbs_logs.get_log_files(osbs, 1)
    for entry in output:
        assert entry[1] == metadata[entry[1]['filename']]


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
    (None, None, True),
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

    inspect_data = {
        'created': 'create_time',
        'os': 'os version',
        'container_config': 'container config',
        'architecture': 'arch',
        'docker_version': 'docker version',
        'config': 'conf',
        'rootfs': 'some roots'
    }
    config_digest = 987654321

    expect_inspect = {
        'Created': 'create_time',
        'Os': 'os version',
        'ContainerConfig': 'container config',
        'Architecture': 'arch',
        'DockerVersion': 'docker version',
        'Config': 'conf',
        'RootFS': 'some roots',
        'Id': config_digest
    }
    if found_versions == ('v1', ):
        config_digest = None
        expect_inspect.pop('RootFS')
        expect_inspect['Id'] = None
        inspect_data.pop('rootfs')

    v2_list_json = {'manifests': [{'mediaType': type_in_list, 'digest': 12345}]}
    v2_list_response = flexmock(json=lambda: v2_list_json, status_code=200)
    v1_json = {'history': [{'v1Compatibility': json.dumps(inspect_data)}]}
    v1_response = flexmock(json=lambda: v1_json, status_code=200)
    v2_response = None

    return_list = {}
    if found_versions:
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
        if found_versions and ('v2' in found_versions or 'v2_list' in found_versions):
            (flexmock(atomic_reactor.util.RegistryClient)
             .should_receive('get_config_and_id_from_registry')
             .and_return(inspect_data, config_digest)
             .once())
        inspected = get_inspect_for_image(image, image.registry, insecure)
        assert inspected == expect_inspect


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
def test_has_operator_manifest(tmpdir, workflow, labels, f_true, f_false):
    df_content = dedent("""\
        FROM fedora
        ENV foo=bar
        """)
    for label in labels:
        df_content += 'LABEL {}\n'.format(label)
    workflow.source = StubSource()
    workflow.builder = StubInsideBuilder()
    cont_path = os.path.join(str(tmpdir), 'Dockerfile')
    with open(cont_path, 'w') as f:
        f.write(df_content)
    workflow.builder.df_path = cont_path

    for func in f_true:
        assert func(workflow), 'Label not properly detected'

    for func in f_false:
        assert not func(workflow), 'Label false positively detected'
