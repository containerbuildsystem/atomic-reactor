"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from collections import namedtuple
import json
import os
from textwrap import dedent
try:
    import koji
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
    import koji

try:
    import atomic_reactor.plugins.post_pulp_sync  # noqa:F401
    PULP_SYNC_AVAILABLE = True
except ImportError:
    PULP_SYNC_AVAILABLE = False

from osbs.build.build_response import BuildResponse
from atomic_reactor.core import DockerTasker
from atomic_reactor.plugins.post_fetch_worker_metadata import FetchWorkerMetadataPlugin
from atomic_reactor.plugins.build_orchestrate_build import (OrchestrateBuildPlugin,
                                                            WORKSPACE_KEY_UPLOAD_DIR,
                                                            WORKSPACE_KEY_BUILD_INFO)
from atomic_reactor.plugins.exit_koji_import import KojiImportPlugin
from atomic_reactor.plugins.exit_koji_tag_build import KojiTagBuildPlugin
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin
from atomic_reactor.plugins.pre_check_and_set_rebuild import CheckAndSetRebuildPlugin
from atomic_reactor.plugins.pre_add_filesystem import AddFilesystemPlugin
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor.plugin import ExitPluginsRunner, PluginFailedException
from atomic_reactor.inner import DockerBuildWorkflow, TagConf, PushConf
from atomic_reactor.util import (ImageName, ManifestDigest,
                                 get_manifest_media_version, get_manifest_media_type)
from atomic_reactor.source import GitSource, PathSource
from atomic_reactor.build import BuildResult
from atomic_reactor.constants import (IMAGE_TYPE_DOCKER_ARCHIVE,
                                      PLUGIN_PULP_PULL_KEY, PLUGIN_PULP_SYNC_KEY,
                                      PLUGIN_GROUP_MANIFESTS_KEY, PLUGIN_KOJI_PARENT_KEY,
                                      PLUGIN_RESOLVE_COMPOSES_KEY, BASE_IMAGE_KOJI_BUILD,
                                      PARENT_IMAGES_KOJI_BUILDS, BASE_IMAGE_BUILD_ID_KEY,
                                      PLUGIN_VERIFY_MEDIA_KEY, PARENT_IMAGE_BUILDS_KEY,
                                      PARENT_IMAGES_KEY, OPERATOR_MANIFESTS_ARCHIVE)
from tests.constants import SOURCE, MOCK
from tests.flatpak import MODULEMD_AVAILABLE, setup_flatpak_source_info
from tests.stubs import StubInsideBuilder, StubSource

from flexmock import flexmock
import pytest
from tests.docker_mock import mock_docker
import subprocess
from osbs.api import OSBS
from osbs.exceptions import OsbsException
from six import string_types

LogEntry = namedtuple('LogEntry', ['platform', 'line'])

NAMESPACE = 'mynamespace'
BUILD_ID = 'build-1'


class X(object):
    pass


class MockedPodResponse(object):
    def get_container_image_ids(self):
        return {'buildroot:latest': '0123456'}


class MockedClientSession(object):
    TAG_TASK_ID = 1234
    DEST_TAG = 'images-candidate'

    def __init__(self, hub, opts=None, task_states=None):
        self.uploaded_files = {}
        self.build_tags = {}
        self.task_states = task_states or ['FREE', 'ASSIGNED', 'CLOSED']

        self.task_states = list(self.task_states)
        self.task_states.reverse()
        self.tag_task_state = self.task_states.pop()
        self.getLoggedInUser = lambda: {'name': 'osbs'}

    def krb_login(self, principal=None, keytab=None, proxyuser=None):
        return True

    def ssl_login(self, cert, ca, serverca, proxyuser=None):
        return True

    def logout(self):
        pass

    def uploadWrapper(self, localfile, path, name=None, callback=None,
                      blocksize=1048576, overwrite=True):
        self.blocksize = blocksize
        with open(localfile, 'rb') as fp:
            self.uploaded_files[name] = fp.read()

    def CGImport(self, metadata, server_dir):
        self.metadata = metadata
        self.server_dir = server_dir
        return {"id": "123"}

    def getBuildTarget(self, target):
        return {'dest_tag_name': self.DEST_TAG}

    def tagBuild(self, tag, build, force=False, fromtag=None):
        self.build_tags[build] = tag
        return self.TAG_TASK_ID

    def getTaskInfo(self, task_id, request=False):
        assert task_id == self.TAG_TASK_ID

        # For extra code coverage, imagine Koji denies the task ever
        # existed.
        if self.tag_task_state is None:
            return None

        return {'state': koji.TASK_STATES[self.tag_task_state]}

    def taskFinished(self, task_id):
        try:
            self.tag_task_state = self.task_states.pop()
        except IndexError:
            # No more state changes
            pass

        return self.tag_task_state in ['CLOSED', 'FAILED', 'CANCELED', None]


FAKE_SIGMD5 = b'0' * 32
FAKE_RPM_OUTPUT = (
    b'name1;1.0;1;x86_64;0;' + FAKE_SIGMD5 + b';(none);'
    b'RSA/SHA256, Mon 29 Jun 2015 13:58:22 BST, Key ID abcdef01234567\n'

    b'gpg-pubkey;01234567;01234567;(none);(none);(none);(none);(none)\n'

    b'gpg-pubkey-doc;01234567;01234567;noarch;(none);' + FAKE_SIGMD5 +
    b';(none);(none)\n'

    b'name2;2.0;2;x86_64;0;' + FAKE_SIGMD5 + b';' +
    b'RSA/SHA256, Mon 29 Jun 2015 13:58:22 BST, Key ID bcdef012345678;(none)\n'
    b'\n')

FAKE_OS_OUTPUT = 'fedora-22'


def fake_subprocess_output(cmd):
    if cmd.startswith('/bin/rpm'):
        return FAKE_RPM_OUTPUT
    elif 'os-release' in cmd:
        return FAKE_OS_OUTPUT
    else:
        raise RuntimeError


class MockedPopen(object):
    def __init__(self, cmd, *args, **kwargs):
        self.cmd = cmd

    def wait(self):
        return 0

    def communicate(self):
        return (fake_subprocess_output(self.cmd), '')


def fake_Popen(cmd, *args, **kwargs):
    return MockedPopen(cmd, *args, **kwargs)


def fake_digest(image):
    tag = image.to_str(registry=False)
    return 'sha256:{0:032x}'.format(len(tag))


def is_string_type(obj):
    return any(isinstance(obj, strtype)
               for strtype in string_types)


class BuildInfo(object):
    def __init__(self, help_file=None, help_valid=True, media_types=None, digests=None):
        annotations = {}
        if media_types:
            annotations['media-types'] = json.dumps(media_types)
        if help_valid:
            annotations['help_file'] = json.dumps(help_file)
        if digests:
            digest_annotation = []
            for digest_item in digests:
                digest_annotation_item = {
                    "version": get_manifest_media_version(digest_item),
                    "digest": digest_item.default,
                }
                digest_annotation.append(digest_annotation_item)
            annotations['digests'] = json.dumps(digest_annotation)

        self.build = BuildResponse({'metadata': {'annotations': annotations}})


def mock_environment(tmpdir, session=None, name=None,
                     component=None, version=None, release=None,
                     source=None, build_process_failed=False,
                     is_rebuild=True, docker_registry=True,
                     pulp_registries=0, blocksize=None,
                     task_states=None, additional_tags=None,
                     has_config=None, add_tag_conf_primaries=True,
                     add_build_result_primaries=False, container_first=False,
                     yum_repourls=None, has_operator_manifests=False):
    if session is None:
        session = MockedClientSession('', task_states=None)
    if source is None:
        source = GitSource('git', 'git://hostname/path')

    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    base_image_id = '123456parent-id'

    workflow.source = StubSource()
    if yum_repourls:
        workflow.all_yum_repourls = yum_repourls
    workflow.builder = StubInsideBuilder().for_workflow(workflow)
    workflow.builder.image_id = '123456imageid'
    workflow.builder.base_image = ImageName(repo='Fedora', tag='22')
    workflow.builder.set_inspection_data({'ParentId': base_image_id})
    setattr(workflow, 'tag_conf', TagConf())
    with open(os.path.join(str(tmpdir), 'Dockerfile'), 'wt') as df:
        df.write('FROM base\n'
                 'LABEL BZComponent={component} com.redhat.component={component}\n'
                 'LABEL Version={version} version={version}\n'
                 'LABEL Release={release} release={release}\n'
                 .format(component=component, version=version, release=release))
        setattr(workflow.builder, 'df_path', df.name)
    if container_first:
        with open(os.path.join(str(tmpdir), 'container.yaml'), 'wt') as container_conf:
            container_conf.write('go:\n'
                                 '  modules:\n'
                                 '    - module: example.com/packagename\n')
    if name and version:
        workflow.tag_conf.add_unique_image('user/test-image:{v}-timestamp'
                                           .format(v=version))
    if name and version and release and add_tag_conf_primaries:
        workflow.tag_conf.add_primary_images(["{0}:{1}-{2}".format(name,
                                                                   version,
                                                                   release),
                                              "{0}:{1}".format(name, version),
                                              "{0}:latest".format(name)])

    if additional_tags:
        workflow.tag_conf.add_primary_images(["{0}:{1}".format(name, tag)
                                              for tag in additional_tags])

    flexmock(subprocess, Popen=fake_Popen)
    flexmock(koji, ClientSession=lambda hub, opts: session)
    flexmock(GitSource)
    logs = [LogEntry(None, 'orchestrator'),
            LogEntry('x86_64', 'Hurray for bacon: \u2017'),
            LogEntry('x86_64', 'line 2')]
    (flexmock(OSBS)
        .should_receive('get_orchestrator_build_logs')
        .with_args(BUILD_ID)
        .and_return(logs))
    (flexmock(OSBS)
        .should_receive('get_pod_for_build')
        .with_args(BUILD_ID)
        .and_return(MockedPodResponse()))
    setattr(workflow, 'source', source)
    setattr(workflow.source, 'lg', X())
    setattr(workflow.source.lg, 'commit_id', '123456')
    setattr(workflow.source.lg, 'git_path', tmpdir.strpath)
    setattr(workflow, 'push_conf', PushConf())
    if docker_registry:
        docker_reg = workflow.push_conf.add_docker_registry('docker.example.com')

        for image in workflow.tag_conf.images:
            tag = image.to_str(registry=False)
            if pulp_registries:
                docker_reg.digests[tag] = ManifestDigest(v1=fake_digest(image),
                                                         v2='sha256:not-used')
            else:
                docker_reg.digests[tag] = ManifestDigest(v1='sha256:not-used',
                                                         v2=fake_digest(image))

            if has_config:
                docker_reg.config = {
                    'config': {'architecture': 'x86_64'},
                    'container_config': {}
                }

    for pulp_registry in range(pulp_registries):
        workflow.push_conf.add_pulp_registry('env', 'crane.example.com:5000')

    with open(os.path.join(str(tmpdir), 'image.tar.xz'), 'wt') as fp:
        fp.write('x' * 2**12)
        setattr(workflow, 'exported_image_sequence', [{'path': fp.name,
                                                       'type': IMAGE_TYPE_DOCKER_ARCHIVE}])

    annotations = {
        'worker-builds': {
            'x86_64': {
                'build': {
                    'build-name': 'build-1-x64_64',
                },
                'metadata_fragment': 'configmap/build-1-x86_64-md',
                'metadata_fragment_key': 'metadata.json',
            },
            'ppc64le': {
                'build': {
                    'build-name': 'build-1-ppc64le',
                },
                'metadata_fragment': 'configmap/build-1-ppc64le-md',
                'metadata_fragment_key': 'metadata.json',
            }
        },
        'repositories': {
            'unique': ['brew-pulp-docker:8888/myproject/hello-world:0.0.1-9'],
            'primary': []
        }
    }

    if name and version and release and add_build_result_primaries:
        annotations['repositories']['primary'] = [
            'brew-pulp-docker:8888/{0}:{1}-{2}'.format(name, version, release),
            'brew-pulp-docker:8888/{0}:{1}'.format(name, version),
            'brew-pulp-docker:8888/{0}:latest'.format(name),
        ]

    if build_process_failed:
        workflow.build_result = BuildResult(logs=["docker build log - \u2018 \u2017 \u2019 \n'"],
                                            fail_reason="not built")
    else:
        workflow.build_result = BuildResult(logs=["docker build log - \u2018 \u2017 \u2019 \n'"],
                                            image_id="id1234",
                                            annotations=annotations)
    workflow.prebuild_plugins_conf = {}
    workflow.prebuild_results[CheckAndSetRebuildPlugin.key] = is_rebuild
    workflow.postbuild_results[PostBuildRPMqaPlugin.key] = [
        "name1;1.0;1;x86_64;0;2000;" + FAKE_SIGMD5.decode() + ";23000;"
        "RSA/SHA256, Tue 30 Aug 2016 00:00:00, Key ID 01234567890abc;(none)",
        "name2;2.0;1;x86_64;0;3000;" + FAKE_SIGMD5.decode() + ";24000"
        "RSA/SHA256, Tue 30 Aug 2016 00:00:00, Key ID 01234567890abd;(none)",
    ]

    workflow.postbuild_results[FetchWorkerMetadataPlugin.key] = {
        'x86_64': {
            'buildroots': [
                {
                    'container': {
                        'type': 'docker',
                        'arch': 'x86_64'
                    },
                    'extra': {
                        'osbs': {
                            'build_id': '12345',
                            'builder_image_id': '67890'
                        }
                    },
                    'content_generator': {
                        'version': '1.6.23',
                        'name': 'atomic-reactor'
                    },
                    'host': {
                        'os': 'Red Hat Enterprise Linux Server 7.3 (Maipo)',
                        'arch': 'x86_64'
                    },
                    'components': [
                        {
                            'name': 'perl-Net-LibIDN',
                            'sigmd5': '1dba38d073ea8f3e6c99cfe32785c81e',
                            'arch': 'x86_64',
                            'epoch': None,
                            'version': '0.12',
                            'signature': '199e2f91fd431d51',
                            'release': '15.el7',
                            'type': 'rpm'
                        },
                        {
                            'name': 'tzdata',
                            'sigmd5': '2255a5807ca7e4d7274995db18e52bea',
                            'arch': 'noarch',
                            'epoch': None,
                            'version': '2017b',
                            'signature': '199e2f91fd431d51',
                            'release': '1.el7',
                            'type': 'rpm'
                        },
                    ],
                    'tools': [
                        {
                            'version': '1.12.6',
                            'name': 'docker'
                        }
                    ],
                    'id': 1
                }
            ],
            'metadata_version': 0,
            'output': [
                {
                    'type': 'log',
                    'arch': 'noarch',
                    'filename': 'openshift-final.log',
                    'filesize': 106690,
                    'checksum': '2efa754467c0d2ea1a98fb8bfe435955',
                    'checksum_type': 'md5',
                    'buildroot_id': 1
                },
                {
                    'type': 'log',
                    'arch': 'noarch',
                    'filename': 'build.log',
                    'filesize': 1660,
                    'checksum': '8198de09fc5940cf7495e2657039ee72',
                    'checksum_type': 'md5',
                    'buildroot_id': 1
                },
                {
                    'extra': {
                        'image': {
                            'arch': 'x86_64'
                        },
                        'docker': {
                            'repositories': [
                                'docker-registry.example.com:8888/myproject/hello-world:unique-tag',
                                'docker-registry.example.com:8888/myproject/hello-world@sha256:...',
                            ],
                            'parent_id': 'sha256:bf203442',
                            'id': '123456',
                        }
                    },
                    'checksum': '58a52e6f3ed52818603c2744b4e2b0a2',
                    'filename': 'test.x86_64.tar.gz',
                    'buildroot_id': 1,
                    'components': [
                        {
                            'name': 'tzdata',
                            'sigmd5': 'd9dc4e4f205428bc08a52e602747c1e9',
                            'arch': 'noarch',
                            'epoch': None,
                            'version': '2016d',
                            'signature': '199e2f91fd431d51',
                            'release': '1.el7',
                            'type': 'rpm'
                        },
                        {
                            'name': 'setup',
                            'sigmd5': 'b1e5ca72c71f94112cd9fb785b95d4da',
                            'arch': 'noarch',
                            'epoch': None,
                            'version': '2.8.71',
                            'signature': '199e2f91fd431d51',
                            'release': '6.el7',
                            'type': 'rpm'
                        },

                    ],
                    'type': 'docker-image',
                    'checksum_type': 'md5',
                    'arch': 'x86_64',
                    'filesize': 71268781
                }
            ]
        }
    }
    if has_operator_manifests:
        manifests_entry = {
            'type': 'log',
            'filename': OPERATOR_MANIFESTS_ARCHIVE,
            'buildroot_id': 1}
        (workflow.postbuild_results[FetchWorkerMetadataPlugin.key]['x86_64']['output']
         .append(manifests_entry))

    workflow.plugin_workspace = {
        OrchestrateBuildPlugin.key: {
            WORKSPACE_KEY_UPLOAD_DIR: 'test-dir',
            WORKSPACE_KEY_BUILD_INFO: {
               'x86_64': BuildInfo(help_file='help.md')
            }
        }
    }

    return tasker, workflow


@pytest.fixture
def os_env(monkeypatch):
    monkeypatch.setenv('BUILD', json.dumps({
        "metadata": {
            "creationTimestamp": "2015-07-27T09:24:00Z",
            "namespace": NAMESPACE,
            "name": BUILD_ID,
            "labels": {},
        }
    }))
    monkeypatch.setenv('OPENSHIFT_CUSTOM_BUILD_BASE_IMAGE', 'buildroot:latest')


def create_runner(tasker, workflow, ssl_certs=False, principal=None,
                  keytab=None, target=None, tag_later=False, reactor_config_map=False,
                  blocksize=None):
    args = {
        'kojihub': '',
        'url': '/',
    }
    koji_map = {
        'hub_url': '',
        'auth': {}
    }

    if ssl_certs:
        args['koji_ssl_certs'] = '/'
        koji_map['auth']['ssl_certs_dir'] = '/'

    if principal:
        args['koji_principal'] = principal
        koji_map['auth']['krb_principal'] = principal

    if keytab:
        args['koji_keytab'] = keytab
        koji_map['auth']['krb_keytab_path'] = keytab

    if target:
        args['target'] = target
        args['poll_interval'] = 0

    if blocksize:
        args['blocksize'] = blocksize

    plugins_conf = [
        {'name': KojiImportPlugin.key, 'args': args},
    ]

    if reactor_config_map:
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig({'version': 1, 'koji': koji_map})

    if target and tag_later:
        plugins_conf.append({'name': KojiTagBuildPlugin.key,
                             'args': {
                                 'kojihub': '',
                                 'target': target,
                                 'poll_interval': 0.01}})
    workflow.exit_plugins_conf = plugins_conf
    runner = ExitPluginsRunner(tasker, workflow, plugins_conf)
    return runner


class TestKojiImport(object):
    def test_koji_import_get_buildroot(self, tmpdir, os_env):
        metadatas = {
            'ppc64le': {
                'buildroots': [
                    {
                        'container': {
                            'type': 'docker',
                            'arch': 'ppc64le'
                        },
                        'id': 1
                    }
                ],
            },
            'x86_64': {
                'buildroots': [
                    {
                        'container': {
                            'type': 'docker',
                            'arch': 'x86_64'
                        },
                        'id': 1
                    }
                ],
            },
        }
        results = [
            {
                'container': {
                    'arch': 'ppc64le',
                    'type': 'docker',
                },
                'id': 'ppc64le-1',
            },
            {
                'container': {
                    'arch': 'x86_64',
                    'type': 'docker',
                },
                'id': 'x86_64-1',
            },
        ]

        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            build_process_failed=True,
                                            name='ns/name',
                                            version='1.0',
                                            release='1')
        plugin = KojiImportPlugin(tasker, workflow, '', '/')

        assert plugin.get_buildroot(metadatas) == results

    def test_koji_import_failed_build(self, tmpdir, os_env, reactor_config_map):  # noqa
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            build_process_failed=True,
                                            name='ns/name',
                                            version='1.0',
                                            release='1')
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        # Must not have importd this build
        assert not hasattr(session, 'metadata')

    def test_koji_import_no_build_env(self, tmpdir, monkeypatch, os_env, reactor_config_map):  # noqa
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1')
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)

        # No BUILD environment variable
        monkeypatch.delenv("BUILD", raising=False)

        with pytest.raises(PluginFailedException) as exc:
            runner.run()
        assert "plugin 'koji_import' raised an exception: KeyError" in str(exc)

    def test_koji_import_no_build_metadata(self, tmpdir, monkeypatch, os_env, reactor_config_map):  # noqa
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1')
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)

        # No BUILD metadata
        monkeypatch.setenv("BUILD", json.dumps({}))
        with pytest.raises(PluginFailedException):
            runner.run()

    def test_koji_import_wrong_source_type(self, tmpdir, os_env, reactor_config_map):  # noqa
        source = PathSource('path', 'file:///dev/null')
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1',
                                            source=source)
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        with pytest.raises(PluginFailedException) as exc:
            runner.run()
        assert "plugin 'koji_import' raised an exception: RuntimeError" in str(exc)

    @pytest.mark.parametrize(('isolated'), [
        False,
        True,
        None
    ])
    def test_isolated_metadata_json(self, tmpdir, monkeypatch, os_env, isolated, reactor_config_map):  # noqa
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            name='ns/name',
                                            version='1.0',
                                            release='1')
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)

        patch = {
            'metadata': {
                'creationTimestamp': '2015-07-27T09:24:00Z',
                'namespace': NAMESPACE,
                'name': BUILD_ID,
                'labels': {
                },
            }
        }

        if isolated is not None:
            patch['metadata']['labels']['isolated'] = isolated

        monkeypatch.setenv("BUILD", json.dumps(patch))

        runner.run()

        build_metadata = session.metadata['build']['extra']['image']['isolated']
        if isolated:
            assert build_metadata is True
        else:
            assert build_metadata is False

    @pytest.mark.parametrize(('koji_task_id', 'expect_success'), [
        (12345, True),
        ('x', False),
    ])
    def test_koji_import_log_task_id(self, tmpdir, monkeypatch, os_env,
                                     caplog, koji_task_id, expect_success,
                                     reactor_config_map):
        session = MockedClientSession('')
        session.getTaskInfo = lambda x: {'owner': 1234}
        session.getUser = lambda x: {'name': 'dev1'}
        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            name='ns/name',
                                            version='1.0',
                                            release='1')
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)

        monkeypatch.setenv("BUILD", json.dumps({
            'metadata': {
                'creationTimestamp': '2015-07-27T09:24:00Z',
                'namespace': NAMESPACE,
                'name': BUILD_ID,
                'labels': {
                    'koji-task-id': str(koji_task_id),
                },
            }
        }))

        runner.run()
        metadata = session.metadata
        assert 'build' in metadata
        build = metadata['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']
        assert isinstance(extra, dict)

        if expect_success:
            assert "Koji Task ID {}".format(koji_task_id) in caplog.text

            assert 'container_koji_task_id' in extra
            extra_koji_task_id = extra['container_koji_task_id']
            assert isinstance(extra_koji_task_id, int)
            assert extra_koji_task_id == koji_task_id
        else:
            assert "invalid task ID" in caplog.text
            assert 'container_koji_task_id' not in extra

    @pytest.mark.parametrize('params', [
        {
            'should_raise': False,
            'principal': None,
            'keytab': None,
        },
        {
            'should_raise': False,
            'principal': 'principal@EXAMPLE.COM',
            'keytab': 'FILE:/var/run/secrets/mysecret',
        },
        {
            'should_raise': True,
            'principal': 'principal@EXAMPLE.COM',
            'keytab': None,
        },
        {
            'should_raise': True,
            'principal': None,
            'keytab': 'FILE:/var/run/secrets/mysecret',
        },
    ])
    def test_koji_import_krb_args(self, tmpdir, params, os_env, reactor_config_map):
        session = MockedClientSession('')
        expectation = flexmock(session).should_receive('krb_login').and_return(True)
        name = 'name'
        version = '1.0'
        release = '1'
        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            name=name,
                                            version=version,
                                            release=release)
        runner = create_runner(tasker, workflow,
                               principal=params['principal'],
                               keytab=params['keytab'], reactor_config_map=reactor_config_map)

        if params['should_raise']:
            expectation.never()
            with pytest.raises(PluginFailedException):
                runner.run()
        else:
            expectation.once()
            runner.run()

    def test_koji_import_krb_fail(self, tmpdir, os_env, reactor_config_map):  # noqa
        session = MockedClientSession('')
        (flexmock(session)
            .should_receive('krb_login')
            .and_raise(RuntimeError)
            .once())
        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            name='ns/name',
                                            version='1.0',
                                            release='1')
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        with pytest.raises(PluginFailedException):
            runner.run()

    def test_koji_import_ssl_fail(self, tmpdir, os_env, reactor_config_map):  # noqa
        session = MockedClientSession('')
        (flexmock(session)
            .should_receive('ssl_login')
            .and_raise(RuntimeError)
            .once())
        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            name='ns/name',
                                            version='1.0',
                                            release='1')
        runner = create_runner(tasker, workflow, ssl_certs=True, reactor_config_map=reactor_config_map)  # noqa
        with pytest.raises(PluginFailedException):
            runner.run()

    @pytest.mark.parametrize('fail_method', [
        'get_orchestrator_build_logs',
        'get_pod_for_build',
    ])
    def test_koji_import_osbs_fail(self, tmpdir, os_env, fail_method, reactor_config_map):
        tasker, workflow = mock_environment(tmpdir,
                                            name='name',
                                            version='1.0',
                                            release='1')
        (flexmock(OSBS)
            .should_receive(fail_method)
            .and_raise(OsbsException))

        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

    @staticmethod
    def check_components(components):
        assert isinstance(components, list)
        assert len(components) > 0
        for component_rpm in components:
            assert isinstance(component_rpm, dict)
            assert set(component_rpm.keys()) == set([
                'type',
                'name',
                'version',
                'release',
                'epoch',
                'arch',
                'sigmd5',
                'signature',
            ])

            assert component_rpm['type'] == 'rpm'
            assert component_rpm['name']
            assert is_string_type(component_rpm['name'])
            assert component_rpm['name'] != 'gpg-pubkey'
            assert component_rpm['version']
            assert is_string_type(component_rpm['version'])
            assert component_rpm['release']
            epoch = component_rpm['epoch']
            assert epoch is None or isinstance(epoch, int)
            assert is_string_type(component_rpm['arch'])
            assert component_rpm['signature'] != '(none)'

    def validate_buildroot(self, buildroot):
        assert isinstance(buildroot, dict)

        assert set(buildroot.keys()) == set([
            'id',
            'host',
            'content_generator',
            'container',
            'tools',
            'components',
            'extra',
        ])

        host = buildroot['host']
        assert isinstance(host, dict)
        assert set(host.keys()) == set([
            'os',
            'arch',
        ])

        assert host['os']
        assert is_string_type(host['os'])
        assert host['arch']
        assert is_string_type(host['arch'])
        assert host['arch'] != 'amd64'

        content_generator = buildroot['content_generator']
        assert isinstance(content_generator, dict)
        assert set(content_generator.keys()) == set([
            'name',
            'version',
        ])

        assert content_generator['name']
        assert is_string_type(content_generator['name'])
        assert content_generator['version']
        assert is_string_type(content_generator['version'])

        container = buildroot['container']
        assert isinstance(container, dict)
        assert set(container.keys()) == set([
            'type',
            'arch',
        ])

        assert container['type'] == 'docker'
        assert container['arch']
        assert is_string_type(container['arch'])

        assert isinstance(buildroot['tools'], list)
        assert len(buildroot['tools']) > 0
        for tool in buildroot['tools']:
            assert isinstance(tool, dict)
            assert set(tool.keys()) == set([
                'name',
                'version',
            ])

            assert tool['name']
            assert is_string_type(tool['name'])
            assert tool['version']
            assert is_string_type(tool['version'])

        self.check_components(buildroot['components'])

        extra = buildroot['extra']
        assert isinstance(extra, dict)
        assert set(extra.keys()) == set([
            'osbs',
        ])

        assert 'osbs' in extra
        osbs = extra['osbs']
        assert isinstance(osbs, dict)
        assert set(osbs.keys()) == set([
            'build_id',
            'builder_image_id',
        ])

        assert is_string_type(osbs['build_id'])
        assert is_string_type(osbs['builder_image_id'])

    def validate_output(self, output, has_config, expect_digest):
        assert isinstance(output, dict)
        assert 'buildroot_id' in output
        assert 'filename' in output
        assert output['filename']
        assert is_string_type(output['filename'])
        assert 'filesize' in output
        assert int(output['filesize']) > 0
        assert 'arch' in output
        assert output['arch']
        assert is_string_type(output['arch'])
        assert 'checksum' in output
        assert output['checksum']
        assert is_string_type(output['checksum'])
        assert 'checksum_type' in output
        assert output['checksum_type'] == 'md5'
        assert is_string_type(output['checksum_type'])
        assert 'type' in output
        if output['type'] == 'log':
            assert set(output.keys()) == set([
                'buildroot_id',
                'filename',
                'filesize',
                'arch',
                'checksum',
                'checksum_type',
                'type',
            ])
            assert output['arch'] == 'noarch'
        else:
            assert set(output.keys()) == set([
                'buildroot_id',
                'filename',
                'filesize',
                'arch',
                'checksum',
                'checksum_type',
                'type',
                'components',
                'extra',
            ])
            assert output['type'] == 'docker-image'
            assert is_string_type(output['arch'])
            assert output['arch'] != 'noarch'
            assert output['arch'] in output['filename']
            self.check_components(output['components'])

            extra = output['extra']
            assert isinstance(extra, dict)
            assert set(extra.keys()) == set([
                'image',
                'docker',
            ])

            image = extra['image']
            assert isinstance(image, dict)
            assert set(image.keys()) == set([
                'arch',
            ])

            assert image['arch'] == output['arch']  # what else?

            assert 'docker' in extra
            docker = extra['docker']
            assert isinstance(docker, dict)
            expected_keys_set = set([
                'parent_id',
                'id',
                'repositories',
                'tags',
            ])
            if has_config:
                expected_keys_set.add('config')
            assert set(docker.keys()) == expected_keys_set

            assert is_string_type(docker['parent_id'])
            assert is_string_type(docker['id'])
            repositories = docker['repositories']
            assert isinstance(repositories, list)
            repositories_digest = list(filter(lambda repo: '@sha256' in repo, repositories))
            assert sorted(repositories_digest) == sorted(set(repositories_digest))

            # if has_config:
            #    config = docker['config']
            #    assert isinstance(config, dict)
            #    assert 'container_config' not in [x.lower() for x in config.keys()]
            #    assert all(is_string_type(entry) for entry in config)

    def test_koji_import_import_fail(self, tmpdir, os_env, caplog, reactor_config_map):  # noqa
        session = MockedClientSession('')
        (flexmock(session)
            .should_receive('CGImport')
            .and_raise(RuntimeError))
        name = 'ns/name'
        version = '1.0'
        release = '1'
        target = 'images-docker-candidate'
        tasker, workflow = mock_environment(tmpdir,
                                            name=name,
                                            version=version,
                                            release=release,
                                            session=session)
        runner = create_runner(tasker, workflow, target=target,
                               reactor_config_map=reactor_config_map)
        with pytest.raises(PluginFailedException):
            runner.run()

        assert 'metadata:' in caplog.text

    @pytest.mark.parametrize(('parent_id', 'expect_success', 'expect_error'), [
        (1234, True, False),
        (None, False, False),
        ('x', False, True),
        ('NO-RESULT', False, False),
    ])
    def test_koji_import_parent_id(self, parent_id, tmpdir, expect_success, os_env, expect_error,
                                   caplog, reactor_config_map):
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1',
                                            session=session)

        koji_parent_result = None
        if parent_id != 'NO-RESULT':
            koji_parent_result = {
                BASE_IMAGE_KOJI_BUILD: {'id': parent_id},
            }
        workflow.prebuild_results[PLUGIN_KOJI_PARENT_KEY] = koji_parent_result

        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        data = session.metadata
        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']
        assert isinstance(extra, dict)

        if expect_error:
            assert 'invalid koji parent id' in caplog.text
        if expect_success:
            image = extra['image']
            assert isinstance(image, dict)
            assert BASE_IMAGE_BUILD_ID_KEY in image
            parent_image_koji_build_id = image[BASE_IMAGE_BUILD_ID_KEY]
            assert isinstance(parent_image_koji_build_id, int)
            assert parent_image_koji_build_id == parent_id
        else:
            if 'image' in extra:
                assert BASE_IMAGE_BUILD_ID_KEY not in extra['image']

    @pytest.mark.parametrize('base_from_scratch', [True, False])  # noqa: F811
    def test_produces_metadata_for_parent_images(
            self, tmpdir, os_env, reactor_config_map, base_from_scratch
        ):

        koji_session = MockedClientSession('')
        tasker, workflow = mock_environment(
            tmpdir, session=koji_session, name='ns/name', version='1.0', release='1'
        )

        koji_parent_result = {
            BASE_IMAGE_KOJI_BUILD: dict(id=16, extra='build info'),
            PARENT_IMAGES_KOJI_BUILDS: {
                ImageName.parse('base'): dict(nvr='base-16.0-1', id=16, extra='build_info'),
            },
        }
        workflow.prebuild_results[PLUGIN_KOJI_PARENT_KEY] = koji_parent_result
        workflow.builder.base_from_scratch = base_from_scratch
        parents_ordered = ['base:latest', 'scratch', 'some:1.0']
        workflow.builder.parents_ordered = parents_ordered

        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        image_metadata = koji_session.metadata['build']['extra']['image']
        key = PARENT_IMAGE_BUILDS_KEY
        assert key in image_metadata
        assert image_metadata[key]['base:latest'] == dict(nvr='base-16.0-1', id=16)
        assert 'extra' not in image_metadata[key]['base:latest']
        key = BASE_IMAGE_BUILD_ID_KEY
        if base_from_scratch:
            assert key not in image_metadata
        else:
            assert key in image_metadata
            assert image_metadata[key] == 16
        key = PARENT_IMAGES_KEY
        assert key in image_metadata
        assert image_metadata[key] == parents_ordered

    @pytest.mark.parametrize(('task_id', 'expect_success'), [
        (1234, True),
        ('x', False),
    ])
    def test_koji_import_filesystem_koji_task_id(self, tmpdir, os_env, caplog, task_id,
                                                 expect_success, reactor_config_map):
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1',
                                            session=session)
        workflow.prebuild_results[AddFilesystemPlugin.key] = {
            'base-image-id': 'abcd',
            'filesystem-koji-task-id': task_id,
        }
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        data = session.metadata
        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']
        assert isinstance(extra, dict)

        if expect_success:
            assert 'filesystem_koji_task_id' in extra
            filesystem_koji_task_id = extra['filesystem_koji_task_id']
            assert isinstance(filesystem_koji_task_id, int)
            assert filesystem_koji_task_id == task_id
        else:
            assert 'invalid task ID' in caplog.text
            assert 'filesystem_koji_task_id' not in extra

    def test_koji_import_filesystem_koji_task_id_missing(self, tmpdir, os_env, caplog,  # noqa
                                                         reactor_config_map):
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1',
                                            session=session)
        workflow.prebuild_results[AddFilesystemPlugin.key] = {
            'base-image-id': 'abcd',
        }
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        data = session.metadata
        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']
        assert isinstance(extra, dict)
        assert 'filesystem_koji_task_id' not in extra
        assert AddFilesystemPlugin.key in caplog.text

    @pytest.mark.parametrize('blocksize', (None, 1048576))
    @pytest.mark.parametrize(('apis',
                              'docker_registry',
                              'pulp_registries',
                              'target'), [
        ('v1-only',
         False,
         1,
         'images-docker-candidate'),

        ('v1+v2',
         True,
         2,
         None),

        ('v2-only',
         True,
         1,
         None),

        ('v1+v2',
         True,
         0,
         None),

    ])
    @pytest.mark.parametrize(('has_config', 'is_autorebuild'), [
        # (True,
        #  True),
        (False,
         False),
    ])
    @pytest.mark.parametrize('tag_later', (True, False))
    @pytest.mark.parametrize(('pulp_pull', 'verify_media', 'expect_id'), (
        (['v1'], False, 'abcdef123456'),
        (['v1', 'v2'], False, 'abc123'),
        (False, ['v1', 'v2', 'v2_list'], 'ab12'),
        (False, ['v1'], 'ab12'),
        (False, False, 'ab12')
    ))
    def test_koji_import_success(self, tmpdir, blocksize, apis,
                                 docker_registry, pulp_registries,
                                 target, os_env, has_config, is_autorebuild,
                                 tag_later, pulp_pull, verify_media, expect_id,
                                 reactor_config_map):
        session = MockedClientSession('')
        # When target is provided koji build will always be tagged,
        # either by koji_import or koji_tag_build.
        (flexmock(session)
            .should_call('tagBuild')
            .with_args('images-candidate', '123')
         )  # .times(1 if target else 0))

        component = 'component'
        name = 'ns/name'
        version = '1.0'
        release = '1'

        if has_config and not docker_registry:
            # Not a valid combination
            has_config = False

        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            name=name,
                                            component=component,
                                            version=version,
                                            release=release,
                                            docker_registry=docker_registry,
                                            pulp_registries=pulp_registries,
                                            has_config=has_config)
        workflow.prebuild_results[CheckAndSetRebuildPlugin.key] = is_autorebuild
        if pulp_pull:
            workflow.exit_results[PLUGIN_PULP_PULL_KEY] = pulp_pull
        elif verify_media:
            workflow.exit_results[PLUGIN_VERIFY_MEDIA_KEY] = verify_media
        expected_media_types = pulp_pull or verify_media or []

        workflow.builder.image_id = expect_id

        runner = create_runner(tasker, workflow, target=target, tag_later=tag_later,
                               reactor_config_map=reactor_config_map,
                               blocksize=blocksize)
        runner.run()

        data = session.metadata

        assert set(data.keys()) == set([
            'metadata_version',
            'build',
            'buildroots',
            'output',
        ])

        assert data['metadata_version'] in ['0', 0]

        build = data['build']
        assert isinstance(build, dict)

        buildroots = data['buildroots']
        assert isinstance(buildroots, list)
        assert len(buildroots) > 0

        output_files = data['output']
        assert isinstance(output_files, list)
        if pulp_pull:
            for output in output_files:
                if 'extra' in output:
                    assert output['extra']['docker']['id'] == expect_id

        assert set(build.keys()) == set([
            'name',
            'version',
            'release',
            'source',
            'start_time',
            'end_time',
            'extra',          # optional but always supplied
            'owner',
        ])

        assert build['name'] == component
        assert build['version'] == version
        assert build['release'] == release
        assert build['source'] == 'git://hostname/path#123456'
        start_time = build['start_time']
        assert isinstance(start_time, int) and start_time
        end_time = build['end_time']
        assert isinstance(end_time, int) and end_time

        extra = build['extra']
        assert isinstance(extra, dict)
        assert 'image' in extra
        image = extra['image']
        assert isinstance(image, dict)
        assert 'autorebuild' in image
        autorebuild = image['autorebuild']
        assert isinstance(autorebuild, bool)
        assert autorebuild == is_autorebuild
        if expected_media_types:
            media_types = image['media_types']
            assert isinstance(media_types, list)
            assert sorted(media_types) == sorted(expected_media_types)

        for buildroot in buildroots:
            self.validate_buildroot(buildroot)

            # Unique within buildroots in this metadata
            assert len([b for b in buildroots
                        if b['id'] == buildroot['id']]) == 1

        for output in output_files:
            self.validate_output(output, has_config, expect_digest=docker_registry)
            buildroot_id = output['buildroot_id']

            # References one of the buildroots
            assert len([buildroot for buildroot in buildroots
                        if buildroot['id'] == buildroot_id]) == 1

        build_id = runner.plugins_results[KojiImportPlugin.key]
        assert build_id == "123"

        assert set(session.uploaded_files.keys()) == set([
            'orchestrator.log',
            'x86_64.log',
        ])
        orchestrator_log = session.uploaded_files['orchestrator.log']
        assert orchestrator_log == b'orchestrator\n'
        x86_64_log = session.uploaded_files['x86_64.log']
        assert x86_64_log.decode('utf-8') == dedent("""\
            Hurray for bacon: \u2017
            line 2
        """)

    def test_koji_import_owner_submitter(self, tmpdir, monkeypatch, reactor_config_map):  # noqa
        session = MockedClientSession('')
        session.getTaskInfo = lambda x: {'owner': 1234}
        session.getUser = lambda x: {'name': 'dev1'}
        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            name='ns/name',
                                            version='1.0',
                                            release='1')
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)

        monkeypatch.setenv("BUILD", json.dumps({
            'metadata': {
                'creationTimestamp': '2015-07-27T09:24:00Z',
                'namespace': NAMESPACE,
                'name': BUILD_ID,
                'labels': {
                    'koji-task-id': 1234,
                },
            }
        }))

        runner.run()
        metadata = session.metadata
        assert metadata['build']['extra']['submitter'] == 'osbs'
        assert metadata['build']['owner'] == 'dev1'

    @pytest.mark.parametrize('use_pulp', [False, True])
    def test_koji_import_pullspec(self, tmpdir, os_env, use_pulp, reactor_config_map):
        session = MockedClientSession('')
        name = 'myproject/hello-world'
        version = '1.0'
        release = '1'
        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            name=name,
                                            version=version,
                                            release=release,
                                            pulp_registries=1,
                                            )
        if use_pulp:
            workflow.postbuild_results[PLUGIN_PULP_SYNC_KEY] = [
                ImageName.parse('crane.example.com:5000/myproject/hello-world:1.0-1'),
            ]

        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        log_outputs = [
            output
            for output in session.metadata['output']
            if output['type'] == 'log'
        ]
        assert log_outputs

        docker_outputs = [
            output
            for output in session.metadata['output']
            if output['type'] == 'docker-image'
        ]
        assert len(docker_outputs) == 1
        docker_output = docker_outputs[0]

        digest_pullspecs = [
            repo
            for repo in docker_output['extra']['docker']['repositories']
            if '@sha256' in repo
        ]
        assert len(digest_pullspecs) == 1

        # Check registry
        reg = set(ImageName.parse(repo).registry
                  for repo in docker_output['extra']['docker']['repositories'])
        assert len(reg) == 1
        if use_pulp:
            assert reg == set(['crane.example.com:5000'])
        else:
            assert reg == set(['docker-registry.example.com:8888'])

    def test_koji_import_without_build_info(self, tmpdir, os_env, reactor_config_map):  # noqa

        class LegacyCGImport(MockedClientSession):

            def CGImport(self, *args, **kwargs):
                super(LegacyCGImport, self).CGImport(*args, **kwargs)
                return

        session = LegacyCGImport('')
        name = 'ns/name'
        version = '1.0'
        release = '1'
        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            name=name,
                                            version=version,
                                            release=release)
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        assert runner.plugins_results[KojiImportPlugin.key] is None

    @pytest.mark.parametrize('expect_result', [
        'empty_config',
        'no_help_file',
        'skip',
        'pass'
    ])
    def test_koji_import_add_help(self, tmpdir, os_env, expect_result, reactor_config_map):
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1',
                                            session=session)

        if expect_result == 'pass':
            workflow.plugin_workspace[OrchestrateBuildPlugin.key][WORKSPACE_KEY_BUILD_INFO]['x86_64'] = BuildInfo('foo.md')  # noqa
        elif expect_result == 'empty_config':
            workflow.plugin_workspace[OrchestrateBuildPlugin.key][WORKSPACE_KEY_BUILD_INFO]['x86_64'] = BuildInfo('help.md')  # noqa
        elif expect_result == 'no_help_file':
            workflow.plugin_workspace[OrchestrateBuildPlugin.key][WORKSPACE_KEY_BUILD_INFO]['x86_64'] = BuildInfo(None)  # noqa
        elif expect_result == 'skip':
            workflow.plugin_workspace[OrchestrateBuildPlugin.key][WORKSPACE_KEY_BUILD_INFO]['x86_64'] = BuildInfo(None, False)  # noqa

        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        data = session.metadata
        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']
        assert isinstance(extra, dict)
        assert 'image' in extra
        image = extra['image']
        assert isinstance(image, dict)

        if expect_result == 'pass':
            assert 'help' in image.keys()
            assert image['help'] == 'foo.md'
        elif expect_result == 'empty_config':
            assert 'help' in image.keys()
            assert image['help'] == 'help.md'
        elif expect_result == 'no_help_file':
            assert 'help' in image.keys()
            assert image['help'] is None
        elif expect_result in ['skip', 'unknown_status']:
            assert 'help' not in image.keys()

    @pytest.mark.skipif(not MODULEMD_AVAILABLE,
                        reason="libmodulemd not available")
    def test_koji_import_flatpak(self, tmpdir, os_env, reactor_config_map):
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1',
                                            session=session)

        setup_flatpak_source_info(workflow)

        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        data = session.metadata
        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']
        assert isinstance(extra, dict)
        assert 'image' in extra
        image = extra['image']
        assert isinstance(image, dict)

        assert image.get('flatpak') is True
        assert image.get('modules') == ['eog-f28-20170629213428',
                                        'flatpak-runtime-f28-20170701152209']
        assert image.get('source_modules') == ['eog:f28']
        assert image.get('odcs') == {
            'signing_intent': 'unsigned',
            'signing_intent_overridden': False,
        }

    @pytest.mark.parametrize(('config', 'expected'), [
        ({'pulp_push': False,
          'pulp_pull_in_worker': False,
          'pulp_pull_in_orchestrator': False,
          'schema1': False,
          'schema2': False,
          'list': False},
         None),
        ({'pulp_push': True,
          'pulp_pull_in_worker': False,
          'pulp_pull_in_orchestrator': False,
          'schema1': False,
          'schema2': False,
          'list': False},
         ["application/json"]),
        ({'pulp_push': True,
          'pulp_pull_in_worker': True,
          'pulp_pull_in_orchestrator': False,
          'schema1': True,
          'schema2': False,
          'list': False},
         ["application/json",
          "application/vnd.docker.distribution.manifest.v1+json"]),
        ({'pulp_push': False,
          'pulp_pull_in_worker': True,
          'pulp_pull_in_orchestrator': False,
          'schema1': True,
          'schema2': False,
          'list': False},
         ["application/vnd.docker.distribution.manifest.v1+json"]),
        ({'pulp_push': True,
          'pulp_pull_in_worker': True,
          'pulp_pull_in_orchestrator': False,
          'schema1': True,
          'schema2': True,
          'list': False},
         ["application/json",
          "application/vnd.docker.distribution.manifest.v1+json",
          "application/vnd.docker.distribution.manifest.v2+json"]),
        ({'pulp_push': True,
          'pulp_pull_in_worker': False,
          'pulp_pull_in_orchestrator': True,
          'schema1': True,
          'schema2': True,
          'list': False},
         ["application/json",
          "application/vnd.docker.distribution.manifest.v1+json",
          "application/vnd.docker.distribution.manifest.v2+json"]),
        ({'pulp_push': True,
          'pulp_pull_in_worker': False,
          'pulp_pull_in_orchestrator': True,
          'schema1': True,
          'schema2': True,
          'list': True},
         ["application/json",
          "application/vnd.docker.distribution.manifest.v1+json",
          "application/vnd.docker.distribution.manifest.v2+json",
          "application/vnd.docker.distribution.manifest.list.v2+json"]),
        ({'pulp_push': True,
          'pulp_pull_in_worker': False,
          'pulp_pull_in_orchestrator': True,
          'schema1': False,
          'schema2': True,
          'list': True},
         ["application/json",
          "application/vnd.docker.distribution.manifest.v2+json",
          "application/vnd.docker.distribution.manifest.list.v2+json"]),
        ({'pulp_push': False,
          'pulp_pull_in_worker': False,
          'pulp_pull_in_orchestrator': True,
          'schema1': True,
          'schema2': True,
          'list': True},
         ["application/vnd.docker.distribution.manifest.v1+json",
          "application/vnd.docker.distribution.manifest.v2+json",
          "application/vnd.docker.distribution.manifest.list.v2+json"]),
    ])
    def test_koji_import_set_media_types(self, tmpdir, os_env, config, expected,
                                         reactor_config_map):
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1',
                                            session=session)
        worker_media_types = []
        if config['pulp_push']:
            worker_media_types += ["application/json"]
        pulp_pull_media_types = []
        if config['schema1']:
            pulp_pull_media_types += ['application/vnd.docker.distribution.manifest.v1+json']
        if config['schema2']:
            pulp_pull_media_types += ['application/vnd.docker.distribution.manifest.v2+json']
        if config['list']:
            pulp_pull_media_types += ['application/vnd.docker.distribution.manifest.list.v2+json']
        if config['pulp_pull_in_worker']:
            worker_media_types += pulp_pull_media_types
        if worker_media_types:
            build_info = BuildInfo(media_types=worker_media_types)
            orchestrate_plugin = workflow.plugin_workspace[OrchestrateBuildPlugin.key]
            orchestrate_plugin[WORKSPACE_KEY_BUILD_INFO]['x86_64'] = build_info
            workflow.postbuild_results[PLUGIN_PULP_SYNC_KEY] = [
                ImageName.parse('crane.example.com/ns/name:1.0-1'),
            ]
        if config['pulp_pull_in_orchestrator']:
            workflow.exit_results[PLUGIN_PULP_PULL_KEY] = pulp_pull_media_types
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        data = session.metadata
        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']
        assert isinstance(extra, dict)
        assert 'image' in extra
        image = extra['image']
        assert isinstance(image, dict)
        if expected:
            assert 'media_types' in image.keys()
            assert sorted(image['media_types']) == sorted(expected)
        else:
            assert 'media_types' not in image.keys()

    @pytest.mark.parametrize('digest', [
        None,
        ManifestDigest(v2='sha256:abcdef345'),
        ManifestDigest(v1='sha256:abcdef678'),
        ManifestDigest(oci='sha256:abcdef901'),
        ManifestDigest(v2='sha256:abcdef123', v1='sha256:abcdef456'),
    ])
    def test_koji_import_set_digests_info(self, tmpdir, os_env, digest, reactor_config_map):
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1',
                                            session=session)
        registry = workflow.push_conf.add_docker_registry('docker.example.com')
        for image in workflow.tag_conf.images:
            tag = image.to_str(registry=False)
            registry.digests[tag] = 'tag'
        for platform, metadata in workflow.postbuild_results[FetchWorkerMetadataPlugin.key].items():
            for output in metadata['output']:
                if output['type'] != 'docker-image':
                    continue

                output['extra']['docker']['repositories'] = [
                    'crane.example.com/foo:tag',
                    'crane.example.com/foo@sha256:bar',
                ]
        workflow.postbuild_results[PLUGIN_GROUP_MANIFESTS_KEY] = {}
        orchestrate_plugin = workflow.plugin_workspace[OrchestrateBuildPlugin.key]
        if digest:
            build_info = BuildInfo(digests=[digest])
        else:
            build_info = BuildInfo()
        orchestrate_plugin[WORKSPACE_KEY_BUILD_INFO]['x86_64'] = build_info

        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        data = session.metadata
        for output in data['output']:
            if output['type'] != 'docker-image':
                continue
            if not digest:
                assert 'digests' not in output['extra']['docker']
            else:
                digest_version = get_manifest_media_version(digest)
                expected_media_type = get_manifest_media_type(digest_version)
                expected_digest_value = digest.default
                expected_digests = {expected_media_type: expected_digest_value}
                assert output['extra']['docker']['digests'] == expected_digests

    @pytest.mark.parametrize('is_scratch', [True, False])
    @pytest.mark.parametrize('digest', [
        None,
        ManifestDigest(v2_list='sha256:e6593f3e'),
    ])
    def test_koji_import_set_manifest_list_info(self, caplog, tmpdir, monkeypatch, os_env,
                                                is_scratch, digest, reactor_config_map):
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1',
                                            session=session,
                                            docker_registry=True,
                                            pulp_registries=1,
                                            add_tag_conf_primaries=not is_scratch)
        group_manifest_result = {'myproject/hello-world': digest} if digest else {}
        workflow.postbuild_results[PLUGIN_GROUP_MANIFESTS_KEY] = group_manifest_result
        orchestrate_plugin = workflow.plugin_workspace[OrchestrateBuildPlugin.key]
        orchestrate_plugin[WORKSPACE_KEY_BUILD_INFO]['x86_64'] = BuildInfo()
        monkeypatch.setenv('BUILD', json.dumps({
            "metadata": {
                "creationTimestamp": "2015-07-27T09:24:00Z",
                "namespace": NAMESPACE,
                "name": BUILD_ID,
                "labels": {'scratch': is_scratch},
            }
        }))
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        if is_scratch:
            medata_tag = '_metadata_'
            metadata_file = 'metadata.json'
            assert metadata_file in session.uploaded_files
            data = json.loads(session.uploaded_files[metadata_file])
            meta_rec = {x.arch: x.message for x in caplog.records if x.arch == medata_tag}
            assert medata_tag in meta_rec
            upload_dir = \
                workflow.plugin_workspace[OrchestrateBuildPlugin.key][WORKSPACE_KEY_UPLOAD_DIR]
            dest_file = os.path.join(upload_dir, metadata_file)
            assert dest_file == meta_rec[medata_tag]
        else:
            data = session.metadata

        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']
        assert isinstance(extra, dict)
        assert 'image' in extra
        image = extra['image']
        assert isinstance(image, dict)
        expected_results = {}
        if is_scratch:
            expected_results['tags'] = [tag.tag
                                        for tag in workflow.tag_conf.images]
        else:
            expected_results['tags'] = [tag.tag
                                        for tag in workflow.tag_conf.primary_images]

        for tag in expected_results['tags']:
            if '-' in tag:
                version_release = tag
                break
        else:
            raise RuntimeError("incorrect test data")

        if digest:
            assert 'index' in image.keys()
            pullspec = "crane.example.com:5000/myproject/hello-world@{0}".format(digest.v2_list)
            expected_results['pull'] = [pullspec]
            pullspec = "crane.example.com:5000/myproject/hello-world:{0}".format(version_release)
            expected_results['pull'].append(pullspec)
            expected_results['digests'] = {
                'application/vnd.docker.distribution.manifest.list.v2+json': digest.v2_list}
            assert image['index'] == expected_results
        else:
            assert 'index' not in image.keys()
            assert 'output' in data
            for output in data['output']:
                if output['type'] == 'log':
                    continue
                assert 'extra' in output
                extra = output['extra']
                assert 'docker' in extra
                assert 'tags' in extra['docker']
                assert sorted(expected_results['tags']) == sorted(extra['docker']['tags'])
                repositories = extra['docker']['repositories']
                assert len(repositories) == 2
                assert len([pullspec for pullspec in repositories
                            if '@' in pullspec]) == 1
                by_tags = [pullspec for pullspec in repositories
                           if '@' not in pullspec]
                assert len(by_tags) == 1
                by_tag = by_tags[0]

                # This test uses a metadata fragment which reports the
                # following registry. In real uses this would really
                # be a Crane registry URI.
                registry = 'docker-registry.example.com:8888'
                assert by_tag == '%s/myproject/hello-world:%s' % (registry,
                                                                  version_release)

    @pytest.mark.skipif(not PULP_SYNC_AVAILABLE,
                        reason="pulp_sync not available")
    @pytest.mark.parametrize('available,expected', [
        (None, ['sha256:v1', 'sha256:v2']),
        (['foo', 'sha256:v1'], ['sha256:v1']),
        (['sha256:v1', 'sha256:v2'], ['sha256:v1', 'sha256:v2']),
    ])
    def test_koji_import_unavailable_manifest_digests(self, tmpdir, os_env,
                                                      available, expected, reactor_config_map):
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1',
                                            session=session)
        registry = workflow.push_conf.add_docker_registry('docker.example.com')
        for image in workflow.tag_conf.images:
            tag = image.to_str(registry=False)
            registry.digests[tag] = ManifestDigest(v1='sha256:v1',
                                                   v2='sha256:v2')

        for platform, metadata in workflow.postbuild_results[FetchWorkerMetadataPlugin.key].items():
            for output in metadata['output']:
                if output['type'] != 'docker-image':
                    continue

                output['extra']['docker']['repositories'] = [
                    'crane.example.com/foo:tag',
                    'crane.example.com/foo@sha256:v1',
                    'crane.example.com/foo@sha256:v2',
                ]

        list_digests = {'myproject/hello-world': ManifestDigest(v2_list='sha256:manifest-list')}
        workflow.postbuild_results[PLUGIN_GROUP_MANIFESTS_KEY] = list_digests
        orchestrate_plugin = workflow.plugin_workspace[OrchestrateBuildPlugin.key]
        orchestrate_plugin[WORKSPACE_KEY_BUILD_INFO]['x86_64'] = BuildInfo()
        if available is not None:
            workflow.plugin_workspace[PLUGIN_PULP_SYNC_KEY] = available

        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        data = session.metadata
        outputs = data['output']
        for output in outputs:
            if output['type'] != 'docker-image':
                continue

            repositories = output['extra']['docker']['repositories']
            repositories = [pullspec.split('@', 1)[1]
                            for pullspec in repositories
                            if '@' in pullspec]
            assert repositories == expected
            break
        else:
            raise RuntimeError("no docker-image output found")

    @pytest.mark.parametrize(('add_tag_conf_primaries', 'add_build_result_primaries', 'success'), (
        (False, False, False),
        (True, False, True),
        (False, True, True),
    ))
    def test_koji_import_primary_images(self, tmpdir, os_env, add_tag_conf_primaries,
                                        add_build_result_primaries, success,
                                        reactor_config_map):
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            name='ns/name',
                                            version='1.0',
                                            release='1',
                                            add_tag_conf_primaries=add_tag_conf_primaries,
                                            add_build_result_primaries=add_build_result_primaries,
                                            )

        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)

        if not success:
            with pytest.raises(PluginFailedException) as exc_info:
                runner.run()
            assert 'Unable to find version-release image' in str(exc_info.value)
            return

        runner.run()

    @pytest.mark.parametrize(('comp', 'sign_int', 'override'), [
        ([{'id': 1}, {'id': 2}, {'id': 3}], "beta", True),
        ([{'id': 2}, {'id': 3}, {'id': 4}], "release", True),
        ([{'id': 3}, {'id': 4}, {'id': 5}], "beta", False),
        ([{'id': 4}, {'id': 5}, {'id': 6}], "release", False),
        (None, None, None)
    ])
    def test_odcs_metadata_koji(self, tmpdir, os_env, comp, sign_int, override,
                                reactor_config_map):
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1',
                                            session=session)

        resolve_comp_entry = False
        if comp is not None and sign_int is not None and override is not None:
            resolve_comp_entry = True

            workflow.prebuild_results[PLUGIN_RESOLVE_COMPOSES_KEY] = {
                'composes': comp,
                'signing_intent': sign_int,
                'signing_intent_overridden': override,
            }

        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        data = session.metadata
        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']
        assert isinstance(extra, dict)
        assert 'image' in extra
        image = extra['image']
        assert isinstance(image, dict)

        if resolve_comp_entry:
            comp_ids = [item['id'] for item in comp]

            assert 'odcs' in image
            odcs = image['odcs']
            assert isinstance(odcs, dict)
            assert odcs['compose_ids'] == comp_ids
            assert odcs['signing_intent'] == sign_int
            assert odcs['signing_intent_overridden'] == override

        else:
            assert 'odcs' not in image

    @pytest.mark.parametrize('resolve_run', [
        True,
        False,
    ])
    def test_odcs_metadata_koji_plugin_run(self, tmpdir, os_env, resolve_run,
                                           reactor_config_map):
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1',
                                            session=session)

        if resolve_run:
            workflow.prebuild_results[PLUGIN_RESOLVE_COMPOSES_KEY] = None

        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        data = session.metadata
        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']
        assert isinstance(extra, dict)
        assert 'image' in extra
        image = extra['image']
        assert isinstance(image, dict)
        assert 'odcs' not in image

    @pytest.mark.parametrize('container_first', [True, False])
    def test_go_metadata(self, tmpdir, os_env, container_first, reactor_config_map):
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1',
                                            session=session,
                                            container_first=container_first)

        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        data = session.metadata
        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']
        assert isinstance(extra, dict)
        assert 'image' in extra
        image = extra['image']
        assert isinstance(image, dict)
        if container_first:
            assert 'go' in image
            go = image['go']
            assert isinstance(go, dict)
            assert 'modules' in go
            modules = go['modules']
            assert isinstance(modules, list)
            assert len(modules) == 1
            module = modules[0]
            assert module['module'] == 'example.com/packagename'
        else:
            assert 'go' not in image

    @pytest.mark.parametrize('yum_repourl', [
        None,
        [],
        ["http://example.com/my.repo", ],
        ["http://example.com/my.repo", "http://example.com/other.repo"],
    ])
    def test_yum_repourls_metadata(self, tmpdir, os_env, yum_repourl, reactor_config_map):
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1',
                                            session=session,
                                            yum_repourls=yum_repourl)

        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        data = session.metadata
        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']
        assert isinstance(extra, dict)
        assert 'image' in extra
        image = extra['image']
        assert isinstance(image, dict)
        if yum_repourl:
            assert 'yum_repourls' in image
            repourls = image['yum_repourls']
            assert isinstance(repourls, list)
            assert repourls == yum_repourl
        else:
            assert 'yum_repourls' not in image

    @pytest.mark.parametrize('has_manifests', [True, False])
    def test_set_operators_metadata(self, tmpdir, os_env, has_manifests, reactor_config_map):
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1',
                                            session=session,
                                            has_operator_manifests=has_manifests)

        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        data = session.metadata
        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']
        assert isinstance(extra, dict)
        if has_manifests:
            assert 'operator_manifests_archive' in extra
            operator_manifests = extra['operator_manifests_archive']
            assert isinstance(operator_manifests, str)
            assert operator_manifests == OPERATOR_MANIFESTS_ARCHIVE
        else:
            assert 'operator_manifests_archive' not in extra
