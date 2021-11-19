"""
Copyright (c) 2015, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from collections import namedtuple
import json
import koji
import os
import yaml
from textwrap import dedent

from atomic_reactor.plugins.post_fetch_worker_metadata import FetchWorkerMetadataPlugin
from atomic_reactor.plugins.build_orchestrate_build import (OrchestrateBuildPlugin,
                                                            WORKSPACE_KEY_UPLOAD_DIR,
                                                            WORKSPACE_KEY_BUILD_INFO)
from atomic_reactor.plugins.exit_koji_import import (KojiImportPlugin,
                                                     KojiImportSourceContainerPlugin)
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin
from atomic_reactor.plugins.pre_add_filesystem import AddFilesystemPlugin
from atomic_reactor.plugins.pre_fetch_sources import PLUGIN_FETCH_SOURCES_KEY
from atomic_reactor.plugin import ExitPluginsRunner, PluginFailedException
from atomic_reactor.inner import DockerBuildWorkflow, TagConf, PushConf, BuildResult
from atomic_reactor.util import (ManifestDigest, DockerfileImages,
                                 get_manifest_media_version, get_manifest_media_type,
                                 graceful_chain_get)
from atomic_reactor.utils import imageutil
from atomic_reactor.source import GitSource, PathSource
from atomic_reactor.constants import (IMAGE_TYPE_DOCKER_ARCHIVE, IMAGE_TYPE_OCI_TAR,
                                      PLUGIN_GENERATE_MAVEN_METADATA_KEY,
                                      PLUGIN_GROUP_MANIFESTS_KEY, PLUGIN_KOJI_PARENT_KEY,
                                      PLUGIN_RESOLVE_COMPOSES_KEY, BASE_IMAGE_KOJI_BUILD,
                                      PARENT_IMAGES_KOJI_BUILDS, BASE_IMAGE_BUILD_ID_KEY,
                                      PLUGIN_PIN_OPERATOR_DIGESTS_KEY,
                                      PLUGIN_PUSH_OPERATOR_MANIFESTS_KEY,
                                      PLUGIN_RESOLVE_REMOTE_SOURCE,
                                      PLUGIN_VERIFY_MEDIA_KEY, PARENT_IMAGE_BUILDS_KEY,
                                      PARENT_IMAGES_KEY, OPERATOR_MANIFESTS_ARCHIVE,
                                      REMOTE_SOURCE_TARBALL_FILENAME,
                                      REMOTE_SOURCE_JSON_FILENAME,
                                      MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                                      MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST,
                                      KOJI_BTYPE_REMOTE_SOURCE_FILE,
                                      KOJI_KIND_IMAGE_BUILD,
                                      KOJI_KIND_IMAGE_SOURCE_BUILD,
                                      KOJI_SUBTYPE_OP_APPREGISTRY,
                                      KOJI_SUBTYPE_OP_BUNDLE,
                                      KOJI_SOURCE_ENGINE)
from tests.flatpak import (MODULEMD_AVAILABLE,
                           setup_flatpak_composes,
                           setup_flatpak_source_info)
from tests.stubs import StubSource
from tests.util import add_koji_map_in_workflow
from tests.constants import OSBS_BUILD_LOG_FILENAME

from flexmock import flexmock
import pytest
import subprocess
from osbs.api import OSBS
from osbs.exceptions import OsbsException
from osbs.utils import ImageName

LogEntry = namedtuple('LogEntry', ['platform', 'line'])

NAMESPACE = 'mynamespace'
PIPELINE_RUN_NAME = 'test-pipeline-run'
SOURCES_FOR_KOJI_NVR = 'component-release-version'
SOURCES_SIGNING_INTENT = 'some_intent'
REMOTE_SOURCE_FILE_FILENAME = 'pnc-sources.tar.gz'

PUSH_OPERATOR_MANIFESTS_RESULTS = {
    "endpoint": 'registry.url/endpoint',
    "registryNamespace": 'test_org',
    "repository": 'test_repo',
    "release": 'test_release',
}


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
        self.task_states = task_states or ['OPEN']

        self.task_states = list(self.task_states)
        self.task_states.reverse()
        self.tag_task_state = self.task_states.pop()
        self.getLoggedInUser = lambda: {'name': 'osbs'}

        self.blocksize = None
        self.server_dir = None
        self.refunded_build = False
        self.fail_state = None

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

    def CGImport(self, metadata, server_dir, token=None):
        # metadata cannot be defined in __init__ because tests assume
        # the attribute will not be defined unless this method is called
        self.metadata = metadata    # pylint: disable=attribute-defined-outside-init
        self.server_dir = server_dir
        return {"id": "123"}

    def CGRefundBuild(self, cg, build_id, token, state):
        self.refunded_build = True
        self.fail_state = state

    def getBuildTarget(self, target):
        return {'dest_tag_name': self.DEST_TAG}

    def getTaskInfo(self, task_id, request=False):
        assert task_id == self.TAG_TASK_ID

        # For extra code coverage, imagine Koji denies the task ever
        # existed.
        if self.tag_task_state is None:
            return None

        return {'state': koji.TASK_STATES[self.tag_task_state], 'owner': 1234}

    def getUser(self, user_id):
        return {'name': 'osbs'}

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
    return isinstance(obj, str)


class MockResponse(object):
    def __init__(self, build_json=None):
        self.json = build_json

    def get_annotations(self):
        return graceful_chain_get(self.json, "metadata", "annotations")


class BuildInfo(object):
    def __init__(self, help_file=None, help_valid=True, media_types=None, digests=None):
        self.annotations = {}
        if media_types:
            self.annotations['media-types'] = json.dumps(media_types)
        if help_valid:
            self.annotations['help_file'] = json.dumps(help_file)
        if digests:
            digest_annotation = []
            for digest_item in digests:
                digest_annotation_item = {
                    "version": get_manifest_media_version(digest_item),
                    "digest": digest_item.default,
                }
                digest_annotation.append(digest_annotation_item)
            self.annotations['digests'] = json.dumps(digest_annotation)

        self.build = MockResponse({'metadata': {'annotations': self.annotations}})


def mock_reactor_config(workflow, allow_multiple_remote_sources=False):
    data = dedent("""\
        version: 1
        koji:
            hub_url: /
            root_url: ''
            auth: {{}}
        allow_multiple_remote_sources: {}
        openshift:
            url: openshift_url
        """.format(allow_multiple_remote_sources))

    config = yaml.safe_load(data)
    workflow.conf.conf = config


def mock_environment(tmpdir, session=None, name=None,
                     component=None, version=None, release=None,
                     source=None, build_process_failed=False, build_process_canceled=False,
                     docker_registry=True, additional_tags=None,
                     has_config=None, add_tag_conf_primaries=True,
                     container_first=False, yum_repourls=None,
                     has_op_appregistry_manifests=False,
                     has_op_bundle_manifests=False,
                     push_operator_manifests_enabled=False, source_build=False,
                     has_remote_source=False, has_remote_source_file=False,
                     has_pnc_build_metadata=False, scratch=False):
    if session is None:
        session = MockedClientSession('')
    if source is None:
        source = GitSource('git', 'git://hostname/path')

    workflow = DockerBuildWorkflow(source=None)
    mock_reactor_config(workflow)
    workflow.user_params['scratch'] = scratch
    base_image_id = '123456parent-id'

    workflow.source = StubSource()
    if yum_repourls:
        workflow.all_yum_repourls = yum_repourls
    workflow.image_id = '123456imageid'
    workflow.dockerfile_images = DockerfileImages(['Fedora:22'])

    flexmock(imageutil).should_receive('base_image_inspect').and_return({'ParentId': base_image_id})
    setattr(workflow, 'tag_conf', TagConf())
    setattr(workflow, 'reserved_build_id ', None)
    setattr(workflow, 'reserved_token', None)
    with open(os.path.join(str(tmpdir), 'Dockerfile'), 'wt') as df:
        df.write('FROM base\n'
                 'LABEL BZComponent={component} com.redhat.component={component}\n'
                 'LABEL Version={version} version={version}\n'
                 'LABEL Release={release} release={release}\n'
                 .format(component=component, version=version, release=release))
        if has_op_appregistry_manifests:
            df.write('LABEL com.redhat.delivery.appregistry=true\n')
        if has_op_bundle_manifests:
            df.write('LABEL com.redhat.delivery.operator.bundle=true\n')
        flexmock(workflow, df_path=df.name)
    if container_first:
        with open(os.path.join(str(tmpdir), 'container.yaml'), 'wt') as container_conf:
            container_conf.write('go:\n'
                                 '  modules:\n'
                                 '    - module: example.com/packagename\n')
    if name and version:
        workflow.tag_conf.add_unique_image('{}:{}-timestamp'.format(name, version))
    if name and version and release and add_tag_conf_primaries:
        workflow.tag_conf.add_primary_image("{0}:{1}-{2}".format(name,
                                                                 version,
                                                                 release))
        workflow.tag_conf.add_floating_images(["{0}:{1}".format(name, version),
                                               "{0}:latest".format(name)])
    if additional_tags:
        workflow.tag_conf.add_floating_images(["{0}:{1}".format(name, tag)
                                              for tag in additional_tags])

    flexmock(subprocess, Popen=fake_Popen)
    flexmock(koji, ClientSession=lambda hub, opts: session)
    (flexmock(GitSource)
        .should_receive('path')
        .and_return(tmpdir))

    logs = {
        "taskRun1": {"containerA": "log message A", "containerB": "log message B"},
        "taskRun2": {"containerC": "log message C"},
    }

    (flexmock(OSBS)
        .should_receive('get_build_logs')
        .with_args(PIPELINE_RUN_NAME)
        .and_return(logs))
    setattr(workflow, 'source', source)
    setattr(workflow.source, 'lg', X())
    setattr(workflow.source.lg, 'commit_id', '123456')
    setattr(workflow.source.lg, 'git_path', tmpdir.strpath)
    setattr(workflow, 'push_conf', PushConf())
    if docker_registry:
        docker_reg = workflow.push_conf.add_docker_registry('docker.example.com')

        for image in workflow.tag_conf.images:
            tag = image.to_str(registry=False)
            docker_reg.digests[tag] = ManifestDigest(v1='sha256:not-used',
                                                     v2=fake_digest(image))

            if has_config:
                docker_reg.config = {
                    'config': {'architecture': 'x86_64'},
                    'container_config': {}
                }

    if source_build:
        exported_file_type = IMAGE_TYPE_OCI_TAR
    else:
        exported_file_type = IMAGE_TYPE_DOCKER_ARCHIVE
    with open(os.path.join(str(tmpdir), 'image.tar.xz'), 'wt') as fp:
        fp.write('x' * 2**12)
        setattr(workflow, 'exported_image_sequence', [{'path': fp.name,
                                                       'type': exported_file_type}])

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
        }
    }

    if build_process_failed:
        workflow.build_result = BuildResult(logs=["docker build log - \u2018 \u2017 \u2019 \n'"],
                                            fail_reason="not built")
        if build_process_canceled:
            workflow.build_canceled = True
    else:
        workflow.build_result = BuildResult(logs=["docker build log - \u2018 \u2017 \u2019 \n'"],
                                            image_id="id1234",
                                            annotations=annotations)
    workflow.prebuild_plugins_conf = {}
    workflow.prebuild_results[PLUGIN_FETCH_SOURCES_KEY] = {'sources_for_nvr': SOURCES_FOR_KOJI_NVR,
                                                           'signing_intent': SOURCES_SIGNING_INTENT}
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
                        'type': 'none',
                        'arch': 'x86_64'
                    },
                    'content_generator': {
                        'version': '1.6.23',
                        'name': 'atomic-reactor'
                    },
                    'host': {
                        'os': 'Red Hat Enterprise Linux Server 7.3 (Maipo)',
                        'arch': 'x86_64'
                    },
                    'id': 1,
                    'components': [],
                    'tools': [],
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
    if has_op_appregistry_manifests or has_op_bundle_manifests:
        manifests_entry = {
            'type': 'log',
            'filename': OPERATOR_MANIFESTS_ARCHIVE,
            'buildroot_id': 1}
        (workflow.postbuild_results[FetchWorkerMetadataPlugin.key]['x86_64']['output']
         .append(manifests_entry))

    if has_remote_source:
        source_path = os.path.join(str(tmpdir), REMOTE_SOURCE_TARBALL_FILENAME)
        with open(source_path, 'wt') as fp:
            fp.write('dummy file')
        remote_source_result = [
            {
                "name": None,
                "url": "https://cachito.com/api/v1/requests/21048/download",
                "remote_source_json": {
                    "filename": REMOTE_SOURCE_JSON_FILENAME,
                    "json": {"stub": "data"},
                },
                "remote_source_tarball": {
                    "filename": REMOTE_SOURCE_TARBALL_FILENAME,
                    "path": source_path,
                },
            }
        ]
        workflow.prebuild_results[PLUGIN_RESOLVE_REMOTE_SOURCE] = remote_source_result
    else:
        workflow.prebuild_results[PLUGIN_RESOLVE_REMOTE_SOURCE] = None

    workflow.postbuild_results[PLUGIN_GENERATE_MAVEN_METADATA_KEY] = None

    if has_remote_source_file:
        filepath = os.path.join(str(tmpdir), REMOTE_SOURCE_FILE_FILENAME)
        with open(filepath, 'wt') as fp:
            fp.write('dummy file')
        remote_source_file_result = {
            'remote_source_files': [
                {
                    'file': filepath,
                    'metadata': {
                        'type': KOJI_BTYPE_REMOTE_SOURCE_FILE,
                        'checksum_type': 'md5',
                        'checksum': '5151c',
                        'filename': REMOTE_SOURCE_FILE_FILENAME,
                        'filesize': os.path.getsize(filepath),
                        'extra': {
                            'source-url': 'example.com/dummy.tar.gz',
                            'artifacts': [
                                {
                                    'url': 'example.com/dummy.jar',
                                    'checksum_type': 'md5',
                                    'checksum': 'abc',
                                    'filename': 'dummy.jar'
                                }
                            ],
                            'typeinfo': {
                                KOJI_BTYPE_REMOTE_SOURCE_FILE: {}
                            },
                        },
                    }
                }
            ],
            'no_source': [{
                'url': 'example.com/dummy-no-source.jar',
                'checksum_type': 'md5',
                'checksum': 'abc',
                'filename': 'dummy-no-source.jar'
            }],
        }
        workflow.postbuild_results[PLUGIN_GENERATE_MAVEN_METADATA_KEY] = remote_source_file_result

    if has_pnc_build_metadata:
        workflow.postbuild_results[PLUGIN_GENERATE_MAVEN_METADATA_KEY] = {
            'pnc_build_metadata': {
                'builds': [
                    {'id': 12345},
                    {'id': 12346}
                ]
            }
        }

    if push_operator_manifests_enabled:
        workflow.postbuild_results[PLUGIN_PUSH_OPERATOR_MANIFESTS_KEY] = \
            PUSH_OPERATOR_MANIFESTS_RESULTS

    workflow.plugin_workspace = {
        OrchestrateBuildPlugin.key: {
            WORKSPACE_KEY_UPLOAD_DIR: 'test-dir',
            WORKSPACE_KEY_BUILD_INFO: {
               'x86_64': BuildInfo(help_file='help.md')
            }
        }
    }

    return workflow


@pytest.fixture
def _user_params(monkeypatch):
    monkeypatch.setattr(DockerBuildWorkflow,
                        '_default_user_params',
                        {'namespace': NAMESPACE,
                         'pipeline_run_name': PIPELINE_RUN_NAME,
                         'koji_task_id': MockedClientSession.TAG_TASK_ID,
                         })


@pytest.fixture
def _os_env(monkeypatch):
    monkeypatch.setenv('OPENSHIFT_CUSTOM_BUILD_BASE_IMAGE', 'buildroot:latest')


def create_runner(workflow, ssl_certs=False, principal=None,
                  keytab=None, target=None, blocksize=None, reserve_build=False,
                  upload_plugin_name=KojiImportPlugin.key, userdata=None):
    args = {}

    if target:
        args['target'] = target
        args['poll_interval'] = 0

    if blocksize:
        args['blocksize'] = blocksize

    if userdata:
        args['userdata'] = userdata

    plugins_conf = [
        {'name': upload_plugin_name, 'args': args},
    ]

    add_koji_map_in_workflow(workflow, hub_url='',
                             reserve_build=reserve_build,
                             ssl_certs_dir='/' if ssl_certs else None,
                             krb_principal=principal,
                             krb_keytab=keytab)

    workflow.exit_plugins_conf = plugins_conf
    runner = ExitPluginsRunner(workflow, plugins_conf)
    return runner


@pytest.mark.usefixtures('user_params')
class TestKojiImport(object):
    def test_koji_import_get_buildroot(self, tmpdir):
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
        workflow = mock_environment(tmpdir, session=session, build_process_failed=True,
                                    name='ns/name', version='1.0', release='1')

        add_koji_map_in_workflow(workflow, hub_url='')

        plugin = KojiImportPlugin(workflow)

        assert plugin.get_buildroot(metadatas) == results

    @pytest.mark.parametrize('reserved_build', [True, False])
    @pytest.mark.parametrize(('canceled_build', 'refund_state'), [
        (True, koji.BUILD_STATES['CANCELED']),
        (False, koji.BUILD_STATES['FAILED']),
    ])
    def test_koji_import_failed_build(self, reserved_build, canceled_build, refund_state,
                                      tmpdir):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, session=session, build_process_failed=True,
                                    build_process_canceled=canceled_build, name='ns/name',
                                    version='1.0', release='1')
        if reserved_build:
            workflow.reserved_build_id = 1
            workflow.reserved_token = 1

        runner = create_runner(workflow, reserve_build=reserved_build)
        runner.run()

        # Must not have importd this build
        assert not hasattr(session, 'metadata')
        if reserved_build:
            assert session.refunded_build
            assert session.fail_state is not None and session.fail_state == refund_state

    def test_koji_import_no_build_metadata(self, tmpdir):
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1')
        runner = create_runner(workflow)

        # No metadata
        workflow.user_params = {}
        with pytest.raises(PluginFailedException):
            runner.run()

    def test_koji_import_wrong_source_type(self, tmpdir, _user_params):
        source = PathSource('path', f'file://{tmpdir}')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1')
        runner = create_runner(workflow)
        setattr(workflow, 'source', source)
        with pytest.raises(PluginFailedException) as exc:
            runner.run()
        assert "plugin 'koji_import' raised an exception: RuntimeError" in str(exc.value)

    @pytest.mark.parametrize(('isolated'), [
        False,
        True,
        None
    ])
    def test_isolated_metadata_json(self, tmpdir, _user_params, isolated):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, session=session, name='ns/name', version='1.0',
                                    release='1')
        runner = create_runner(workflow)

        if isolated is not None:
            workflow.user_params['isolated'] = isolated

        runner.run()

        build_metadata = session.metadata['build']['extra']['image']['isolated']
        if isolated:
            assert build_metadata is True
        else:
            assert build_metadata is False

    @pytest.mark.parametrize(('userdata'), [
        None,
        {},
        {'custom': 'userdata'},
    ])
    def test_userdata_metadata(self, tmpdir, _user_params, userdata):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, session=session, name='ns/name',
                                    version='1.0', release='1')
        runner = create_runner(workflow, userdata=userdata)

        runner.run()

        build_extra_metadata = session.metadata['build']['extra']

        if userdata:
            assert build_extra_metadata['custom_user_metadata'] == userdata
        else:
            assert 'custom_user_metadata' not in build_extra_metadata

    @pytest.mark.parametrize(('koji_task_id', 'expect_success'), [
        (12345, True),
        ('x', False),
    ])
    def test_koji_import_log_task_id(self, tmpdir, _user_params,
                                     caplog, koji_task_id, expect_success):
        session = MockedClientSession('')
        session.getTaskInfo = lambda x: {'owner': 1234, 'state': 1}
        setattr(session, 'getUser', lambda x: {'name': 'dev1'})

        workflow = mock_environment(tmpdir, session=session, name='ns/name', version='1.0',
                                    release='1')
        runner = create_runner(workflow)

        workflow.user_params['koji_task_id'] = koji_task_id

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
    def test_koji_import_krb_args(self, tmpdir, params, _user_params):
        session = MockedClientSession('')
        expectation = flexmock(session).should_receive('krb_login').and_return(True)
        name = 'name'
        version = '1.0'
        release = '1'
        workflow = mock_environment(tmpdir, session=session, name=name, version=version,
                                    release=release)
        runner = create_runner(workflow, principal=params['principal'], keytab=params['keytab'])

        if params['should_raise']:
            expectation.never()
            with pytest.raises(PluginFailedException):
                runner.run()
        else:
            expectation.once()
            runner.run()

    def test_koji_import_krb_fail(self, tmpdir):
        session = MockedClientSession('')
        (flexmock(session)
            .should_receive('krb_login')
            .and_raise(RuntimeError)
            .once())
        workflow = mock_environment(tmpdir, session=session, name='ns/name', version='1.0',
                                    release='1')
        runner = create_runner(workflow)
        with pytest.raises(PluginFailedException):
            runner.run()

    def test_koji_import_ssl_fail(self, tmpdir):
        session = MockedClientSession('')
        (flexmock(session)
            .should_receive('ssl_login')
            .and_raise(RuntimeError)
            .once())
        workflow = mock_environment(tmpdir, session=session, name='ns/name', version='1.0',
                                    release='1')
        runner = create_runner(workflow, ssl_certs=True)
        with pytest.raises(PluginFailedException):
            runner.run()

    @pytest.mark.parametrize('fail_method', [
        'get_build_logs',
    ])
    def test_koji_import_osbs_fail(self, tmpdir, _user_params, fail_method):
        workflow = mock_environment(tmpdir, name='name', version='1.0', release='1')
        (flexmock(OSBS)
            .should_receive(fail_method)
            .and_raise(OsbsException))

        runner = create_runner(workflow)
        runner.run()

    @staticmethod
    def check_components(components):
        assert isinstance(components, list)
        assert len(components) > 0
        for component_rpm in components:
            assert isinstance(component_rpm, dict)
            assert set(component_rpm.keys()) == {
                'type',
                'name',
                'version',
                'release',
                'epoch',
                'arch',
                'sigmd5',
                'signature',
            }

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

    def validate_buildroot(self, buildroot, source=False):
        assert isinstance(buildroot, dict)

        assert set(buildroot.keys()) == {
            'id',
            'host',
            'content_generator',
            'container',
            'components',
            'tools',
        }

        host = buildroot['host']
        assert isinstance(host, dict)
        assert set(host.keys()) == {'os', 'arch'}

#        assert host['os']
#        assert is_string_type(host['os'])
        assert host['arch']
        assert is_string_type(host['arch'])
        assert host['arch'] != 'amd64'

        content_generator = buildroot['content_generator']
        assert isinstance(content_generator, dict)
        assert set(content_generator.keys()) == {'name', 'version'}

        assert content_generator['name']
        assert is_string_type(content_generator['name'])
        assert content_generator['version']
        assert is_string_type(content_generator['version'])

        container = buildroot['container']
        assert isinstance(container, dict)
        assert set(container.keys()) == {'type', 'arch'}

        assert container['type'] == 'none'
        assert container['arch']
        assert is_string_type(container['arch'])

    def validate_output(self, output, has_config, source=False):
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
            assert set(output.keys()) == {
                'buildroot_id',
                'filename',
                'filesize',
                'arch',
                'checksum',
                'checksum_type',
                'type',
            }
            assert output['arch'] == 'noarch'
        else:
            assert set(output.keys()) == {
                'buildroot_id',
                'filename',
                'filesize',
                'arch',
                'checksum',
                'checksum_type',
                'type',
                'components',
                'extra',
            }
            assert output['type'] == 'docker-image'
            assert is_string_type(output['arch'])
            assert output['arch'] != 'noarch'
            assert output['arch'] in output['filename']
            if not source:
                self.check_components(output['components'])
            else:
                assert output['components'] == []

            extra = output['extra']
            assert isinstance(extra, dict)
            assert set(extra.keys()) == {'image', 'docker'}

            image = extra['image']
            assert isinstance(image, dict)
            assert set(image.keys()) == {'arch'}

            assert image['arch'] == output['arch']  # what else?

            assert 'docker' in extra
            docker = extra['docker']
            assert isinstance(docker, dict)
            if source:
                expected_keys_set = {
                    'tags',
                    'digests',
                    'layer_sizes',
                    'repositories',
                    'id',
                }
            else:
                expected_keys_set = {
                    'parent_id',
                    'id',
                    'repositories',
                    'tags',
                    'floating_tags',
                    'unique_tags',
                }
            if has_config:
                expected_keys_set.add('config')

            assert set(docker.keys()) == expected_keys_set

            if not source:
                assert is_string_type(docker['parent_id'])
            assert is_string_type(docker['id'])
            repositories = docker['repositories']
            assert isinstance(repositories, list)
            repositories_digest = list(filter(lambda repo: '@sha256' in repo, repositories))
            assert sorted(repositories_digest) == sorted(set(repositories_digest))

    def test_koji_import_import_fail(self, tmpdir, caplog, _user_params):
        session = MockedClientSession('')
        (flexmock(session)
            .should_receive('CGImport')
            .and_raise(RuntimeError))
        name = 'ns/name'
        version = '1.0'
        release = '1'
        target = 'images-docker-candidate'
        workflow = mock_environment(tmpdir, name=name, version=version, release=release,
                                    session=session)
        runner = create_runner(workflow, target=target)
        with pytest.raises(PluginFailedException):
            runner.run()

        assert 'metadata:' in caplog.text

    @pytest.mark.parametrize(('parent_id', 'expect_success', 'expect_error'), [
        (1234, True, False),
        (None, False, False),
        ('x', False, True),
        ('NO-RESULT', False, False),
    ])
    def test_koji_import_parent_id(self, parent_id, tmpdir, expect_success, expect_error,
                                   caplog, _user_params):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session)

        koji_parent_result = None
        if parent_id != 'NO-RESULT':
            koji_parent_result = {
                BASE_IMAGE_KOJI_BUILD: {'id': parent_id},
            }
        workflow.prebuild_results[PLUGIN_KOJI_PARENT_KEY] = koji_parent_result

        runner = create_runner(workflow)
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
    def test_produces_metadata_for_parent_images(self, tmpdir, base_from_scratch, _user_params):

        koji_session = MockedClientSession('')
        workflow = mock_environment(tmpdir, session=koji_session, name='ns/name', version='1.0',
                                    release='1')

        koji_parent_result = {
            BASE_IMAGE_KOJI_BUILD: dict(id=16, extra='build info'),
            PARENT_IMAGES_KOJI_BUILDS: {
                ImageName.parse('base'): dict(nvr='base-16.0-1', id=16, extra='build_info'),
            },
        }
        workflow.prebuild_results[PLUGIN_KOJI_PARENT_KEY] = koji_parent_result

        dockerfile_images = ['base:latest', 'scratch', 'some:1.0']
        if base_from_scratch:
            dockerfile_images.append('scratch')
        workflow.dockerfile_images = DockerfileImages(dockerfile_images)

        runner = create_runner(workflow)
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
        assert image_metadata[key] == dockerfile_images

    @pytest.mark.parametrize(('task_id', 'expect_success'), [
        (1234, True),
        ('x', False),
    ])
    def test_koji_import_filesystem_koji_task_id(self, tmpdir, caplog, task_id,
                                                 expect_success, _user_params):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session)
        workflow.prebuild_results[AddFilesystemPlugin.key] = {
            'base-image-id': 'abcd',
            'filesystem-koji-task-id': task_id,
        }
        runner = create_runner(workflow)
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

    def test_koji_import_filesystem_koji_task_id_missing(self, tmpdir, caplog, _user_params):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session)
        workflow.prebuild_results[AddFilesystemPlugin.key] = {
            'base-image-id': 'abcd',
        }
        runner = create_runner(workflow)
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
    @pytest.mark.parametrize(('verify_media', 'expect_id'), (
        (['v1', 'v2', 'v2_list'], 'ab12'),
        (['v1'], 'ab12'),
        (False, 'ab12')
    ))
    @pytest.mark.parametrize('reserved_build', (True, False))
    @pytest.mark.parametrize(('task_states', 'skip_import'), [
        (['OPEN'], False),
        (['FAILED'], True),
    ])
    def test_koji_import_success(self, tmpdir, caplog, blocksize, verify_media,
                                 expect_id, reserved_build, task_states,
                                 skip_import, _user_params):
        session = MockedClientSession('', task_states=task_states)
        component = 'component'
        name = 'ns/name'
        version = '1.0'
        release = '1'

        workflow = mock_environment(tmpdir, session=session, name=name, component=component,
                                    version=version, release=release)

        if verify_media:
            workflow.exit_results[PLUGIN_VERIFY_MEDIA_KEY] = verify_media
        expected_media_types = verify_media or []

        workflow.image_id = expect_id

        build_token = 'token_12345'
        build_id = '123'
        if reserved_build:
            workflow.reserved_build_id = build_id
            workflow.reserved_token = build_token

        if reserved_build:
            (flexmock(session)
                .should_call('CGImport')
                .with_args(dict, str, token=build_token)
             )
        else:
            (flexmock(session)
                .should_call('CGImport')
                .with_args(dict, str)
             )

        target = 'images-docker-candidate'
        runner = create_runner(workflow, target=target, blocksize=blocksize,
                               reserve_build=reserved_build)
        runner.run()

        if skip_import:
            log_msg = "Koji task is not in Open state, but in {}, not importing build".\
                format(task_states[0])

            assert log_msg in caplog.text

            if reserved_build:
                assert session.refunded_build
            return

        data = session.metadata

        assert set(data.keys()) == {
            'metadata_version',
            'build',
            'buildroots',
            'output',
        }

        assert data['metadata_version'] in ['0', 0]

        build = data['build']
        assert isinstance(build, dict)

        buildroots = data['buildroots']
        assert isinstance(buildroots, list)
        assert len(buildroots) > 0

        output_files = data['output']
        assert isinstance(output_files, list)

        expected_keys = {
            'name',
            'version',
            'release',
            'source',
            'start_time',
            'end_time',
            'extra',          # optional but always supplied
            'owner',
        }

        if reserved_build:
            expected_keys.add('build_id')

        assert set(build.keys()) == expected_keys

        if reserved_build:
            assert build['build_id'] == build_id
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

        assert 'osbs_build' in extra
        osbs_build = extra['osbs_build']
        assert isinstance(osbs_build, dict)
        assert 'kind' in osbs_build
        assert osbs_build['kind'] == KOJI_KIND_IMAGE_BUILD
        assert 'subtypes' in osbs_build
        assert osbs_build['subtypes'] == []
        assert 'engine' in osbs_build
        assert osbs_build['engine'] == 'podman'

        assert 'image' in extra
        image = extra['image']
        assert isinstance(image, dict)

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
            self.validate_output(output, False)
            buildroot_id = output['buildroot_id']

            # References one of the buildroots
            assert len([buildroot for buildroot in buildroots
                        if buildroot['id'] == buildroot_id]) == 1

        build_id = runner.plugins_results[KojiImportPlugin.key]
        assert build_id == "123"

        assert set(session.uploaded_files.keys()) == {OSBS_BUILD_LOG_FILENAME}
        orchestrator_log = session.uploaded_files[OSBS_BUILD_LOG_FILENAME]
        assert orchestrator_log == b"log message A\nlog message B\nlog message C\n"
        assert workflow.labels['koji-build-id'] == '123'

    def test_koji_import_owner_submitter(self, tmpdir, _user_params):
        session = MockedClientSession('')
        session.getTaskInfo = lambda x: {'owner': 1234, 'state': 1}
        setattr(session, 'getUser', lambda x: {'name': 'dev1'})

        workflow = mock_environment(tmpdir, session=session, name='ns/name', version='1.0',
                                    release='1')
        runner = create_runner(workflow)
        workflow.user_params['koji_task_id'] = 1234

        runner.run()
        metadata = session.metadata
        assert metadata['build']['extra']['submitter'] == 'osbs'
        assert metadata['build']['owner'] == 'dev1'

    def test_koji_import_pullspec(self, tmpdir, _user_params):
        session = MockedClientSession('')
        name = 'myproject/hello-world'
        version = '1.0'
        release = '1'
        workflow = mock_environment(tmpdir, session=session, name=name, version=version,
                                    release=release)
        runner = create_runner(workflow)
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
        assert reg == {'docker-registry.example.com:8888'}

    def test_koji_import_without_build_info(self, tmpdir, _user_params):

        class LegacyCGImport(MockedClientSession):

            def CGImport(self, metadata, server_dir, token=None):
                super(LegacyCGImport, self).CGImport(metadata, server_dir, token)
                return

        session = LegacyCGImport('')
        name = 'ns/name'
        version = '1.0'
        release = '1'
        workflow = mock_environment(tmpdir, session=session, name=name, version=version,
                                    release=release)
        runner = create_runner(workflow)
        runner.run()

        assert runner.plugins_results[KojiImportPlugin.key] is None

    @pytest.mark.parametrize('expect_result', [
        'empty_config',
        'no_help_file',
        'skip',
        'pass'
    ])
    def test_koji_import_add_help(self, tmpdir, expect_result, _user_params):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session)

        if expect_result == 'pass':
            workflow.plugin_workspace[OrchestrateBuildPlugin.key][WORKSPACE_KEY_BUILD_INFO]['x86_64'] = BuildInfo('foo.md')  # noqa
        elif expect_result == 'empty_config':
            workflow.plugin_workspace[OrchestrateBuildPlugin.key][WORKSPACE_KEY_BUILD_INFO]['x86_64'] = BuildInfo('help.md')  # noqa
        elif expect_result == 'no_help_file':
            workflow.plugin_workspace[OrchestrateBuildPlugin.key][WORKSPACE_KEY_BUILD_INFO]['x86_64'] = BuildInfo(None)  # noqa
        elif expect_result == 'skip':
            workflow.plugin_workspace[OrchestrateBuildPlugin.key][WORKSPACE_KEY_BUILD_INFO]['x86_64'] = BuildInfo(None, False)  # noqa

        runner = create_runner(workflow)
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
    def test_koji_import_flatpak(self, tmpdir, _user_params):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session)

        setup_flatpak_composes(workflow)
        setup_flatpak_source_info(workflow)

        runner = create_runner(workflow)
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
        assert 'osbs_build' in extra
        osbs_build = extra['osbs_build']
        assert osbs_build['subtypes'] == ['flatpak']

        assert image.get('flatpak') is True
        assert image.get('modules') == ['eog-f28-20170629213428',
                                        'flatpak-runtime-f28-20170701152209']
        assert image.get('source_modules') == ['eog:f28']
        assert image.get('odcs') == {
            'compose_ids': [22422, 42],
            'signing_intent': 'unsigned',
            'signing_intent_overridden': False,
        }

    @pytest.mark.parametrize(('config', 'expected'), [
        ({'schema1': False,
          'schema2': False,
          'list': False},
         None),
        ({'schema1': True,
          'schema2': False,
          'list': False},
         ["application/vnd.docker.distribution.manifest.v1+json"]),
        ({'schema1': True,
          'schema2': True,
          'list': False},
         ["application/vnd.docker.distribution.manifest.v1+json",
          "application/vnd.docker.distribution.manifest.v2+json"]),
        ({'schema1': True,
          'schema2': True,
          'list': True},
         ["application/vnd.docker.distribution.manifest.v1+json",
          "application/vnd.docker.distribution.manifest.v2+json",
          "application/vnd.docker.distribution.manifest.list.v2+json"]),
    ])
    def test_koji_import_set_media_types(self, tmpdir, config, expected, _user_params):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session)
        worker_media_types = []
        if config['schema1']:
            worker_media_types += ['application/vnd.docker.distribution.manifest.v1+json']
        if config['schema2']:
            worker_media_types += ['application/vnd.docker.distribution.manifest.v2+json']
        if config['list']:
            worker_media_types += ['application/vnd.docker.distribution.manifest.list.v2+json']
        if worker_media_types:
            build_info = BuildInfo(media_types=worker_media_types)
            orchestrate_plugin = workflow.plugin_workspace[OrchestrateBuildPlugin.key]
            orchestrate_plugin[WORKSPACE_KEY_BUILD_INFO]['x86_64'] = build_info

        runner = create_runner(workflow)
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
    def test_koji_import_set_digests_info(self, tmpdir, digest, _user_params):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session)
        registry = workflow.push_conf.add_docker_registry('docker.example.com')
        for image in workflow.tag_conf.images:
            tag = image.to_str(registry=False)
            registry.digests[tag] = 'tag'
        for metadata in workflow.postbuild_results[FetchWorkerMetadataPlugin.key].values():
            for output in metadata['output']:
                if output['type'] != 'docker-image':
                    continue

                output['extra']['docker']['repositories'] = [
                    'crane.example.com/foo:tag',
                    'crane.example.com/foo@sha256:bar',
                ]
        workflow.postbuild_results[PLUGIN_GROUP_MANIFESTS_KEY] = {
            "media_type": MEDIA_TYPE_DOCKER_V2_SCHEMA2
        }
        orchestrate_plugin = workflow.plugin_workspace[OrchestrateBuildPlugin.key]
        if digest:
            build_info = BuildInfo(digests=[digest])
        else:
            build_info = BuildInfo()
        orchestrate_plugin[WORKSPACE_KEY_BUILD_INFO]['x86_64'] = build_info

        runner = create_runner(workflow)
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
    def test_koji_import_set_manifest_list_info(self, caplog, tmpdir,
                                                is_scratch, digest, _user_params):
        session = MockedClientSession('')
        version = '1.0'
        release = '1'
        name = 'ns/name'
        unique_tag = "{}-timestamp".format(version)
        workflow = mock_environment(tmpdir, name=name, version=version, release=release,
                                    session=session, docker_registry=True,
                                    add_tag_conf_primaries=not is_scratch, scratch=is_scratch)
        group_manifest_result = {"media_type": MEDIA_TYPE_DOCKER_V2_SCHEMA2}
        if digest:
            group_manifest_result = {
                'media_type': MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST,
                'manifest_digest': digest
            }
        workflow.postbuild_results[PLUGIN_GROUP_MANIFESTS_KEY] = group_manifest_result
        orchestrate_plugin = workflow.plugin_workspace[OrchestrateBuildPlugin.key]
        orchestrate_plugin[WORKSPACE_KEY_BUILD_INFO]['x86_64'] = BuildInfo()
        runner = create_runner(workflow)
        runner.run()

        if is_scratch:
            medata_tag = '_metadata_'
            metadata_file = 'metadata.json'
            assert metadata_file in session.uploaded_files
            data = json.loads(session.uploaded_files[metadata_file])
            meta_rec = {x.arch: x.message for x in caplog.records if hasattr(x, 'arch')
                        and x.arch == medata_tag}
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
        expected_results = {'unique_tags': [unique_tag]}
        expected_results['floating_tags'] = [tag.tag for tag in workflow.tag_conf.floating_images]
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
            pullspec = "docker.example.com/{}@{}".format(name, digest.v2_list)
            expected_results['pull'] = [pullspec]
            pullspec = "docker.example.com/{}:{}".format(name, version_release)
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
                assert 'floating_tags' in extra['docker']
                assert 'unique_tags' in extra['docker']
                assert sorted(expected_results['tags']) == sorted(extra['docker']['tags'])
                assert (sorted(expected_results['floating_tags']) ==
                        sorted(extra['docker']['floating_tags']))
                assert (sorted(expected_results['unique_tags']) ==
                        sorted(extra['docker']['unique_tags']))
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

    @pytest.mark.parametrize('available,expected', [
        (None, ['sha256:v1', 'sha256:v2']),
        (['foo', 'sha256:v1'], ['sha256:v1', 'sha256:v2']),
        (['sha256:v1', 'sha256:v2'], ['sha256:v1', 'sha256:v2']),
    ])
    def test_koji_import_unavailable_manifest_digests(self, tmpdir,
                                                      available, expected, _user_params):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session)
        registry = workflow.push_conf.add_docker_registry('docker.example.com')
        for image in workflow.tag_conf.images:
            tag = image.to_str(registry=False)
            registry.digests[tag] = ManifestDigest(v1='sha256:v1',
                                                   v2='sha256:v2')

        for metadata in workflow.postbuild_results[FetchWorkerMetadataPlugin.key].values():
            for output in metadata['output']:
                if output['type'] != 'docker-image':
                    continue

                output['extra']['docker']['repositories'] = [
                    'crane.example.com/foo:tag',
                    'crane.example.com/foo@sha256:v1',
                    'crane.example.com/foo@sha256:v2',
                ]

        workflow.postbuild_results[PLUGIN_GROUP_MANIFESTS_KEY] = {}

        orchestrate_plugin = workflow.plugin_workspace[OrchestrateBuildPlugin.key]
        orchestrate_plugin[WORKSPACE_KEY_BUILD_INFO]['x86_64'] = BuildInfo()

        runner = create_runner(workflow)
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

    @pytest.mark.parametrize(('add_tag_conf_primaries', 'success'), (
        (False, False),
        (True, True),
    ))
    def test_koji_import_primary_images(self, tmpdir, add_tag_conf_primaries,
                                        success, _user_params):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, session=session, name='ns/name', version='1.0',
                                    release='1', add_tag_conf_primaries=add_tag_conf_primaries)

        runner = create_runner(workflow)

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
    def test_odcs_metadata_koji(self, tmpdir, comp, sign_int, override, _user_params):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session)

        resolve_comp_entry = False
        if comp is not None and sign_int is not None and override is not None:
            resolve_comp_entry = True

            workflow.prebuild_results[PLUGIN_RESOLVE_COMPOSES_KEY] = {
                'composes': comp,
                'signing_intent': sign_int,
                'signing_intent_overridden': override,
            }

        runner = create_runner(workflow)
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
    def test_odcs_metadata_koji_plugin_run(self, tmpdir, resolve_run, _user_params):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session)

        if resolve_run:
            workflow.prebuild_results[PLUGIN_RESOLVE_COMPOSES_KEY] = None

        runner = create_runner(workflow)
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
    def test_go_metadata(self, tmpdir, container_first, _user_params):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session, container_first=container_first)

        runner = create_runner(workflow)
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
    def test_yum_repourls_metadata(self, tmpdir, yum_repourl, _user_params):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session, yum_repourls=yum_repourl)

        runner = create_runner(workflow)
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

    @pytest.mark.parametrize('has_appregistry_manifests', [True, False])
    @pytest.mark.parametrize('has_bundle_manifests', [True, False])
    @pytest.mark.parametrize('push_operator_manifests', [True, False])
    def test_set_operators_metadata(
            self, tmpdir,
            has_appregistry_manifests, has_bundle_manifests,
            push_operator_manifests, _user_params):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session,
                                    has_op_appregistry_manifests=has_appregistry_manifests,
                                    has_op_bundle_manifests=has_bundle_manifests,
                                    push_operator_manifests_enabled=push_operator_manifests)
        runner = create_runner(workflow)
        runner.run()

        data = session.metadata
        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']

        assert isinstance(extra, dict)
        assert 'osbs_build' in extra
        osbs_build = extra['osbs_build']
        if has_appregistry_manifests or has_bundle_manifests:
            assert 'operator_manifests_archive' in extra
            operator_manifests = extra['operator_manifests_archive']
            assert isinstance(operator_manifests, str)
            assert operator_manifests == OPERATOR_MANIFESTS_ARCHIVE
            assert 'typeinfo' in extra
            assert 'operator-manifests' in extra['typeinfo']
            operator_typeinfo = extra['typeinfo']['operator-manifests']
            assert isinstance(operator_typeinfo, dict)
            assert operator_typeinfo['archive'] == OPERATOR_MANIFESTS_ARCHIVE
        else:
            assert 'operator_manifests_archive' not in extra
            assert 'typeinfo' not in extra

        # having manifests pushed without extraction cannot happen, but plugins handles
        # results independently so test it this way
        if push_operator_manifests:
            assert extra['operator_manifests']['appregistry'] == PUSH_OPERATOR_MANIFESTS_RESULTS
        else:
            assert 'operator_manifests' not in extra

        assert osbs_build['subtypes'] == [
            stype for yes, stype in [
                (has_appregistry_manifests, KOJI_SUBTYPE_OP_APPREGISTRY),
                (has_bundle_manifests, KOJI_SUBTYPE_OP_BUNDLE)
            ] if yes
        ]

    @pytest.mark.usefixtures('_os_env')
    @pytest.mark.parametrize('has_bundle_manifests', [True, False])
    def test_operators_bundle_metadata(
            self, tmpdir, has_bundle_manifests, _user_params):
        """Test if metadata (extra.image.operator_manifests) about operator
        bundles are properly exported"""
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session, has_op_bundle_manifests=has_bundle_manifests)

        if has_bundle_manifests:
            workflow.prebuild_results[PLUGIN_PIN_OPERATOR_DIGESTS_KEY] = {
                'custom_csv_modifications_applied': False,
                'related_images': {
                    'pullspecs': [
                        {
                            'original': ImageName.parse('old-registry/ns/spam:1'),
                            'new': ImageName.parse('new-registry/new-ns/new-spam@sha256:4'),
                            'pinned': True,
                            'replaced': True
                        }, {
                            'original': ImageName.parse('old-registry/ns/spam@sha256:4'),
                            'new': ImageName.parse('new-registry/new-ns/new-spam@sha256:4'),
                            'pinned': False,
                            'replaced': True
                        }, {
                            'original': ImageName.parse(
                                'registry.private.example.com/ns/foo@sha256:1'),
                            'new': ImageName.parse('registry.private.example.com/ns/foo@sha256:1'),
                            'pinned': False,
                            'replaced': False
                        },
                    ],
                    'created_by_osbs': True,
                }
            }

        runner = create_runner(workflow)
        runner.run()

        data = session.metadata
        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']

        assert isinstance(extra, dict)
        if has_bundle_manifests:
            assert 'operator_manifests' in extra['image']
            expected = {
                'custom_csv_modifications_applied': False,
                'related_images': {
                    'pullspecs': [
                        {
                            'original': 'old-registry/ns/spam:1',
                            'new': 'new-registry/new-ns/new-spam@sha256:4',
                            'pinned': True,
                        }, {
                            'original': 'old-registry/ns/spam@sha256:4',
                            'new': 'new-registry/new-ns/new-spam@sha256:4',
                            'pinned': False,
                        }, {
                            'original': 'registry.private.example.com/ns/foo@sha256:1',
                            'new': 'registry.private.example.com/ns/foo@sha256:1',
                            'pinned': False,
                        },
                    ],
                    'created_by_osbs': True,
                }
            }
            assert extra['image']['operator_manifests'] == expected
        else:
            assert 'operator_manifests' not in extra['image']

    @pytest.mark.usefixtures('_os_env')
    @pytest.mark.parametrize('has_op_csv_modifications', [True, False])
    def test_operators_bundle_metadata_csv_modifications(
            self, tmpdir, has_op_csv_modifications, _user_params):
        """Test if metadata (extra.image.operator_manifests.custom_csv_modifications_applied)
        about operator bundles are properly exported"""
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session, has_op_bundle_manifests=True)

        plugin_res = {
            'custom_csv_modifications_applied': has_op_csv_modifications,
            'related_images': {
                'pullspecs': [],
                'created_by_osbs': True,
            }
        }
        workflow.prebuild_results[PLUGIN_PIN_OPERATOR_DIGESTS_KEY] = plugin_res

        runner = create_runner(workflow)
        runner.run()

        data = session.metadata
        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']

        assert isinstance(extra, dict)
        assert 'operator_manifests' in extra['image']
        expected = {
            'custom_csv_modifications_applied': has_op_csv_modifications,
            'related_images': {
                'pullspecs': [],
                'created_by_osbs': True,
            }
        }

        assert extra['image']['operator_manifests'] == expected

    @pytest.mark.parametrize('has_remote_source', [True, False])
    @pytest.mark.parametrize('allow_multiple_remote_sources', [True, False])
    def test_remote_sources(self, tmpdir, has_remote_source,
                            allow_multiple_remote_sources, _user_params):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session, has_remote_source=has_remote_source)
        mock_reactor_config(workflow, allow_multiple_remote_sources)

        runner = create_runner(workflow)
        runner.run()

        data = session.metadata
        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']
        assert isinstance(extra, dict)
        # https://github.com/PyCQA/pylint/issues/2186
        # pylint: disable=W1655
        if has_remote_source:
            if allow_multiple_remote_sources:
                assert extra['image']['remote_sources'] == [
                    {
                        'name': None,
                        'url': 'https://cachito.com/api/v1/requests/21048',
                    }
                ]
                assert 'typeinfo' in extra
                assert 'remote-sources' in extra['typeinfo']
                assert extra['typeinfo']['remote-sources'] == [
                    {
                        'name': None,
                        'url': 'https://cachito.com/api/v1/requests/21048',
                        'archives': ['remote-source.json', 'remote-source.tar.gz'],
                    }
                ]
                assert REMOTE_SOURCE_TARBALL_FILENAME in session.uploaded_files.keys()
                assert REMOTE_SOURCE_JSON_FILENAME in session.uploaded_files.keys()
            else:
                assert (
                        extra['image']['remote_source_url']
                        == 'https://cachito.com/api/v1/requests/21048/download'
                )
                assert extra['typeinfo']['remote-sources'] == {
                    "remote_source_url": "https://cachito.com/api/v1/requests/21048/download"
                }
                assert REMOTE_SOURCE_TARBALL_FILENAME in session.uploaded_files.keys()
                assert REMOTE_SOURCE_JSON_FILENAME in session.uploaded_files.keys()

        else:
            assert 'remote_source_url' not in extra['image']
            assert 'typeinfo' not in extra
            assert REMOTE_SOURCE_TARBALL_FILENAME not in session.uploaded_files.keys()
            assert REMOTE_SOURCE_JSON_FILENAME not in session.uploaded_files.keys()

    @pytest.mark.parametrize('has_remote_source_file', [True, False])
    def test_remote_source_files(self, tmpdir, has_remote_source_file, _user_params):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session, has_remote_source_file=has_remote_source_file)

        runner = create_runner(workflow)
        runner.run()

        data = session.metadata
        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']
        assert isinstance(extra, dict)
        # https://github.com/PyCQA/pylint/issues/2186
        # pylint: disable=W1655
        if has_remote_source_file:
            assert 'typeinfo' in extra
            assert 'remote-source-file' in extra['typeinfo']
            assert REMOTE_SOURCE_FILE_FILENAME in session.uploaded_files.keys()
        else:
            assert 'typeinfo' not in extra
            assert REMOTE_SOURCE_FILE_FILENAME not in session.uploaded_files.keys()

    @pytest.mark.parametrize('has_pnc_build_metadata', [True, False])
    def test_pnc_build_metadata(self, tmpdir, has_pnc_build_metadata, _user_params):
        session = MockedClientSession('')
        workflow = mock_environment(tmpdir, name='ns/name', version='1.0', release='1',
                                    session=session, has_pnc_build_metadata=has_pnc_build_metadata)

        runner = create_runner(workflow)
        runner.run()

        data = session.metadata
        assert 'build' in data
        build = data['build']
        assert isinstance(build, dict)
        assert 'extra' in build
        extra = build['extra']
        assert isinstance(extra, dict)
        # https://github.com/PyCQA/pylint/issues/2186
        # pylint: disable=W1655
        if has_pnc_build_metadata:
            assert 'pnc' in extra['image']
            assert 'builds' in extra['image']['pnc']
            for build in extra['image']['pnc']['builds']:
                assert 'id' in build
        else:
            assert 'pnc' not in extra['image']

    @pytest.mark.parametrize('blocksize', (None, 1048576))
    @pytest.mark.parametrize('has_config', (True, False))
    @pytest.mark.parametrize(('verify_media', 'expect_id'), (
        (['v1', 'v2', 'v2_list'], 'ab12'),
        (['v1'], 'ab12'),
        (False, 'ab12')
    ))
    @pytest.mark.parametrize('reserved_build', (True, False))
    @pytest.mark.parametrize(('task_states', 'skip_import'), [
        (['OPEN'], False),
        (['FAILED'], True),
    ])
    @pytest.mark.parametrize(('userdata'), [
        None,
        {},
        {'custom': 'userdata'},
    ])
    def test_koji_import_success_source(self, tmpdir, caplog, blocksize, has_config,
                                        verify_media, expect_id, reserved_build, task_states,
                                        skip_import, _user_params, userdata):
        session = MockedClientSession('', task_states=task_states)
        # When target is provided koji build will always be tagged,
        # either by koji_import or koji_tag_build.
        component = 'component'
        name = 'ns/name'
        version = '1.0'
        release = '1'

        workflow = mock_environment(tmpdir, session=session, name=name, component=component,
                                    version=version, release=release, has_config=has_config,
                                    source_build=True)

        workflow.build_result = BuildResult(source_docker_archive="oci_path")
        workflow.koji_source_nvr = {'name': component, 'version': version, 'release': release}
        workflow.koji_source_source_url = 'git://hostname/path#123456'

        if verify_media:
            workflow.exit_results[PLUGIN_VERIFY_MEDIA_KEY] = verify_media
        expected_media_types = verify_media or []

        workflow.image_id = expect_id

        build_token = 'token_12345'
        build_id = '123'
        if reserved_build:
            workflow.reserved_build_id = build_id
            workflow.reserved_token = build_token

        if reserved_build:
            (flexmock(session)
                .should_call('CGImport')
                .with_args(dict, str, token=build_token)
             )
        else:
            (flexmock(session)
                .should_call('CGImport')
                .with_args(dict, str)
             )

        target = 'images-docker-candidate'
        source_manifest = {
            'config': {
                'digest': expect_id,
            },
            'layers': [
                {'size': 20000,
                 'digest': 'sha256:123456789'},
                {'size': 30000,
                 'digest': 'sha256:987654321'},
            ]
        }
        workflow.koji_source_manifest = source_manifest

        runner = create_runner(workflow, target=target, blocksize=blocksize,
                               reserve_build=reserved_build,
                               upload_plugin_name=KojiImportSourceContainerPlugin.key,
                               userdata=userdata)
        runner.run()

        if skip_import:
            log_msg = "Koji task is not in Open state, but in {}, not importing build".\
                format(task_states[0])

            assert log_msg in caplog.text

            if reserved_build:
                assert session.refunded_build
            return

        data = session.metadata

        assert set(data.keys()) == {
            'metadata_version',
            'build',
            'buildroots',
            'output',
        }

        assert data['metadata_version'] in ['0', 0]

        build = data['build']
        assert isinstance(build, dict)

        buildroots = data['buildroots']
        assert isinstance(buildroots, list)
        assert len(buildroots) > 0

        output_files = data['output']
        assert isinstance(output_files, list)

        expected_keys = {
            'name',
            'version',
            'release',
            'source',
            'start_time',
            'end_time',
            'extra',          # optional but always supplied
            'owner',
        }

        if reserved_build:
            expected_keys.add('build_id')

        assert set(build.keys()) == expected_keys

        if reserved_build:
            assert build['build_id'] == build_id
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

        if userdata:
            assert extra['custom_user_metadata'] == userdata
        else:
            assert 'custom_user_metadata' not in extra

        assert 'osbs_build' in extra
        osbs_build = extra['osbs_build']
        assert isinstance(osbs_build, dict)
        assert 'kind' in osbs_build
        assert osbs_build['kind'] == KOJI_KIND_IMAGE_SOURCE_BUILD
        assert 'subtypes' in osbs_build
        assert osbs_build['subtypes'] == []
        assert 'engine' in osbs_build
        assert osbs_build['engine'] == KOJI_SOURCE_ENGINE

        assert 'image' in extra
        image = extra['image']
        assert isinstance(image, dict)

        assert image['sources_for_nvr'] == SOURCES_FOR_KOJI_NVR
        assert image['sources_signing_intent'] == SOURCES_SIGNING_INTENT

        if expected_media_types:
            media_types = image['media_types']
            assert isinstance(media_types, list)
            assert sorted(media_types) == sorted(expected_media_types)

        for buildroot in buildroots:
            self.validate_buildroot(buildroot, source=True)

            # Unique within buildroots in this metadata
            assert len([b for b in buildroots
                        if b['id'] == buildroot['id']]) == 1

        for output in output_files:
            self.validate_output(output, has_config, source=True)
            buildroot_id = output['buildroot_id']

            # References one of the buildroots
            assert len([buildroot for buildroot in buildroots
                        if buildroot['id'] == buildroot_id]) == 1

        build_id = runner.plugins_results[KojiImportSourceContainerPlugin.key]
        assert build_id == "123"

        uploaded_oic_file = 'oci-image-{}.{}.tar.xz'.format(expect_id, os.uname()[4])
        assert set(session.uploaded_files.keys()) == {
            OSBS_BUILD_LOG_FILENAME,
            uploaded_oic_file,
        }
        orchestrator_log = session.uploaded_files[OSBS_BUILD_LOG_FILENAME]
        assert orchestrator_log == b"log message A\nlog message B\nlog message C\n"

        assert workflow.labels['koji-build-id'] == '123'
