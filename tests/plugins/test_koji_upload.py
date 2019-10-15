"""
Copyright (c) 2017, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import json
import os
import platform
import rpm
import sys
import tempfile
import zipfile

try:
    import koji
except ImportError:
    import inspect

    # Find our mocked koji module
    import tests.koji as koji
    mock_koji_path = os.path.dirname(inspect.getfile(koji.ClientSession))
    if mock_koji_path not in sys.path:
        sys.path.append(os.path.dirname(mock_koji_path))

    # Now load it properly, the same way the plugin will
    del koji
    import koji

from atomic_reactor.constants import (
    IMAGE_TYPE_DOCKER_ARCHIVE,
    PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY,
    OPERATOR_MANIFESTS_ARCHIVE)
from atomic_reactor.core import DockerTasker
from atomic_reactor.plugins.post_koji_upload import (KojiUploadLogger,
                                                     KojiUploadPlugin)
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException

from atomic_reactor.inner import DockerBuildWorkflow, TagConf, PushConf
from atomic_reactor.util import ImageName, ManifestDigest
from atomic_reactor.rpm_util import parse_rpm_output
from atomic_reactor.source import GitSource
from atomic_reactor.build import BuildResult
from tests.constants import SOURCE, MOCK
from tests.stubs import StubInsideBuilder, StubSource

from flexmock import flexmock
import pytest
from tests.docker_mock import mock_docker
import subprocess
from osbs.api import OSBS
from osbs.exceptions import OsbsException
from six import string_types

NAMESPACE = 'mynamespace'
BUILD_ID = 'build-1'
KOJI_UPLOAD_DIR = 'upload'
LOCAL_ARCH = platform.processor()


def noop(*args, **kwargs): return None


# temp workaround until this API is added to osbs-client
OSBS.create_config_map = noop
OSBS.get_config_map = noop


class X(object):
    pass


class MockedOSBS(OSBS):
    def __init__(self, logs_return_bytes=True):
        self.configmap = {}

        if logs_return_bytes:
            logs = b'build logs - \xe2\x80\x98 \xe2\x80\x97 \xe2\x80\x99'
        else:
            logs = 'build logs - \u2018 \u2017 \u2019'

        (flexmock(OSBS)
            .should_receive('get_build_logs')
            .with_args(BUILD_ID)
            .and_return(logs))
        (flexmock(OSBS)
            .should_receive('get_pod_for_build')
            .with_args(BUILD_ID)
            .and_return(MockedPodResponse()))
        (flexmock(OSBS)
            .should_receive('create_config_map')
            .with_args(BUILD_ID+'-md', dict)
            .replace_with(self.create_config_map))
        (flexmock(OSBS)
            .should_receive('get_config_map')
            .with_args(BUILD_ID+'-md')
            .replace_with(self.get_config_map))

    def create_config_map(self, name, data):
        assert isinstance(data, dict)
        assert is_string_type(name)

        self.configmap[name] = data

    def get_config_map(self, name):
        assert name in self.configmap

        return self.configmap[name]


class MockedPodResponse(object):
    def get_container_image_ids(self):
        return {'buildroot:latest': '0123456'}


class MockedClientSession(object):
    TAG_TASK_ID = 1234
    DEST_TAG = 'images-candidate'

    def __init__(self, hub, opts=None, task_states=None):
        self.uploaded_files = []
        self.build_tags = {}
        self.task_states = task_states or ['FREE', 'ASSIGNED', 'CLOSED']

        self.task_states = list(self.task_states)
        self.task_states.reverse()
        self.tag_task_state = self.task_states.pop()

        self.blocksize = None
        self.metadata = None
        self.server_dir = None

    def krb_login(self, principal=None, keytab=None, proxyuser=None):
        return True

    def ssl_login(self, cert, ca, serverca, proxyuser=None):
        return True

    def logout(self):
        pass

    def uploadWrapper(self, localfile, path, name=None, callback=None,
                      blocksize=1048576, overwrite=True):
        self.uploaded_files.append(name)
        self.blocksize = blocksize
        assert path.split(os.path.sep, 1)[0] == KOJI_UPLOAD_DIR

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


class MockedRpmHeader(object):
    def __init__(self, name, version, release, arch=None, epoch=None, md5=None, pgp=None, gpg=None):
        self.tags = {'NAME': name, 'VERSION': version, 'RELEASE': release, 'ARCH': arch,
                     'EPOCH': epoch, 'SIGMD5': md5, 'SIGPGP:pgpsig': pgp, 'SIGGPG:pgpsig': gpg}

    def sprintf(self, qf):
        for k, v in self.tags.items():
            if k in qf:
                if v is None:
                    v = '(none)'
                return v


class MockedTS(object):
    def dbMatch(self):
        return [
                MockedRpmHeader(
                    'name1', '1.0', '1', LOCAL_ARCH, '0', FAKE_SIGMD5,
                    gpg='RSA/SHA256, Mon 29 Jun 2015 13:58:22 BST, Key ID abcdef01234567'),
                MockedRpmHeader(
                    'gpg-pubkey', '01234567', '01234567'),
                MockedRpmHeader(
                    'gpg-pubkey-doc', '01234567', '01234567', 'noarch', md5=FAKE_SIGMD5),
                MockedRpmHeader(
                    'name2', '2.0', '2', LOCAL_ARCH, '0', FAKE_SIGMD5,
                    'RSA/SHA256, Mon 29 Jun 2015 13:58:22 BST, Key ID bcdef012345678')]


FAKE_SIGMD5 = '0' * 32

FAKE_OS_OUTPUT = 'fedora-22'


def fake_subprocess_output(cmd):
    if 'os-release' in cmd:
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


def mock_environment(tmpdir, session=None, name=None,
                     component=None, version=None, release=None,
                     source=None, build_process_failed=False,
                     blocksize=None, task_states=None,
                     additional_tags=None, has_config=None):
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
    workflow.builder = StubInsideBuilder().for_workflow(workflow)
    workflow.builder.image_id = '123456imageid'
    workflow.builder.set_inspection_data({'Id': base_image_id})
    setattr(workflow, 'tag_conf', TagConf())
    with open(os.path.join(str(tmpdir), 'Dockerfile'), 'wt') as df:
        df.write('FROM base\n'
                 'LABEL BZComponent={component} com.redhat.component={component}\n'
                 'LABEL Version={version} version={version}\n'
                 'LABEL Release={release} release={release}\n'
                 .format(component=component, version=version, release=release))
        workflow.builder.set_df_path(df.name)
    if name and version:
        workflow.tag_conf.add_unique_image('user/test-image:{v}-timestamp'
                                           .format(v=version))
    if name and version and release:
        workflow.tag_conf.add_primary_images(["{0}:{1}-{2}".format(name,
                                                                   version,
                                                                   release),
                                              "{0}:{1}".format(name, version),
                                              "{0}:latest".format(name)])

    if additional_tags:
        workflow.tag_conf.add_primary_images(["{0}:{1}".format(name, tag)
                                              for tag in additional_tags])

    flexmock(subprocess, Popen=fake_Popen)
    flexmock(rpm, TransactionSet=MockedTS)
    flexmock(koji, ClientSession=lambda hub, opts: session)
    flexmock(GitSource)
    setattr(workflow, 'source', source)
    setattr(workflow.source, 'lg', X())
    setattr(workflow.source.lg, 'commit_id', '123456')
    setattr(workflow, 'push_conf', PushConf())
    docker_reg = workflow.push_conf.add_docker_registry('docker.example.com')

    for image in workflow.tag_conf.images:
        tag = image.to_str(registry=False)

        docker_reg.digests[tag] = ManifestDigest(v1='sha256:not-used',
                                                 v2=fake_digest(image))

        if has_config:
            docker_reg.config = {
                'config': {'architecture': LOCAL_ARCH},
                'container_config': {}
            }

    with open(os.path.join(str(tmpdir), 'image.tar.xz'), 'wt') as fp:
        fp.write('x' * 2**12)
        setattr(workflow, 'exported_image_sequence', [{'path': fp.name,
                                                       'type': IMAGE_TYPE_DOCKER_ARCHIVE}])

    if build_process_failed:
        workflow.build_result = BuildResult(logs=["docker build log - \u2018 \u2017 \u2019 \n'"],
                                            fail_reason="not built")
    else:
        workflow.build_result = BuildResult(logs=["docker build log - \u2018 \u2017 \u2019 \n'"],
                                            image_id="id1234")
    workflow.prebuild_plugins_conf = {}

    workflow.image_components = parse_rpm_output([
        "name1;1.0;1;" + LOCAL_ARCH + ";0;2000;" + FAKE_SIGMD5 + ";23000;"
        "RSA/SHA256, Tue 30 Aug 2016 00:00:00, Key ID 01234567890abc;(none)",
        "name2;2.0;1;" + LOCAL_ARCH + ";0;3000;" + FAKE_SIGMD5 + ";24000"
        "RSA/SHA256, Tue 30 Aug 2016 00:00:00, Key ID 01234567890abd;(none)",
    ])

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
                  keytab=None, blocksize=None, target=None,
                  platform=None,
                  multiple=None, reactor_config_map=False):
    args = {
        'kojihub': '',
        'url': '/',
        'build_json_dir': '',
        'koji_upload_dir': KOJI_UPLOAD_DIR,
    }
    full_conf = {
        'version': 1,
        'openshift': {'url': '/', 'build_json_dir': ''},
    }
    koji_map = {
        'hub_url': '',
        'root_url': '/',
        'auth': {}
    }

    if ssl_certs:
        args['koji_ssl_certs_dir'] = '/'
        koji_map['auth']['ssl_certs_dir'] = '/'

    if principal:
        args['koji_principal'] = principal
        koji_map['auth']['krb_principal'] = principal

    if keytab:
        args['koji_keytab'] = keytab
        koji_map['auth']['krb_keytab_path'] = keytab

    if blocksize:
        args['blocksize'] = blocksize

    if target:
        args['target'] = target
        args['poll_interval'] = 0

    if platform is not None:
        args['platform'] = platform

    if multiple is not None:
        args['report_multiple_digests'] = multiple

    if reactor_config_map:
        full_conf['koji'] = koji_map
        del args['kojihub']
        del args['url']
        del args['build_json_dir']
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig(full_conf)

    plugins_conf = [
        {'name': KojiUploadPlugin.key, 'args': args},
    ]

    workflow.postbuild_plugins_conf = plugins_conf
    runner = PostBuildPluginsRunner(tasker, workflow, plugins_conf)
    return runner


def get_metadata(workflow, osbs):
    cm_annotations = workflow.postbuild_results[KojiUploadPlugin.key]

    if not cm_annotations:
        return {}

    assert "metadata_fragment" in cm_annotations
    assert "metadata_fragment_key" in cm_annotations

    cmlen = len("configmap/")
    cm_key = cm_annotations["metadata_fragment"][cmlen:]

    cm_frag_key = cm_annotations["metadata_fragment_key"]
    cm_data = osbs.get_config_map(cm_key)

    return cm_data[cm_frag_key]


class MockedReactorConfig(object):
    conf = {}

    def __getitem__(self, *args):
        return self


class TestKojiUploadLogger(object):
    @pytest.mark.parametrize('totalsize', [0, 1024])
    def test_with_zero(self, totalsize):
        logger = flexmock()
        logger.should_receive('debug').once()
        upload_logger = KojiUploadLogger(logger)
        upload_logger.callback(0, totalsize, 0, 0, 0)

    @pytest.mark.parametrize(('totalsize', 'step', 'expected_times'), [
        (10, 1, 11),
        (12, 1, 7),
        (12, 3, 5),
    ])
    def test_with_defaults(self, totalsize, step, expected_times):
        logger = flexmock()
        logger.should_receive('debug').times(expected_times)
        upload_logger = KojiUploadLogger(logger)
        upload_logger.callback(0, totalsize, 0, 0, 0)
        for offset in range(step, totalsize + step, step):
            upload_logger.callback(offset, totalsize, step, 1.0, 1.0)

    @pytest.mark.parametrize(('totalsize', 'step', 'notable', 'expected_times'), [
        (10, 1, 10, 11),
        (10, 1, 20, 6),
        (10, 1, 25, 5),
        (12, 3, 25, 5),
    ])
    def test_with_notable(self, totalsize, step, notable, expected_times):
        logger = flexmock()
        logger.should_receive('debug').times(expected_times)
        upload_logger = KojiUploadLogger(logger, notable_percent=notable)
        for offset in range(0, totalsize + step, step):
            upload_logger.callback(offset, totalsize, step, 1.0, 1.0)


class TestKojiUpload(object):
    def test_koji_upload_failed_build(self, tmpdir, os_env, reactor_config_map):  # noqa
        session = MockedClientSession('')
        osbs = MockedOSBS()
        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            build_process_failed=True,
                                            name='ns/name',
                                            version='1.0',
                                            release='1')
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        # Must not have uploaded this build
        metadata = get_metadata(workflow, osbs)
        assert not metadata

    def test_koji_upload_no_tagconf(self, tmpdir, os_env, reactor_config_map):  # noqa
        tasker, workflow = mock_environment(tmpdir)
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        with pytest.raises(PluginFailedException):
            runner.run()

    def test_koji_upload_no_build_env(self, tmpdir, monkeypatch, os_env, reactor_config_map):  # noqa
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1')
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)

        # No BUILD environment variable
        monkeypatch.delenv("BUILD", raising=False)

        with pytest.raises(PluginFailedException) as exc:
            runner.run()
        assert "plugin 'koji_upload' raised an exception: KeyError" in str(exc.value)

    def test_koji_upload_no_build_metadata(self, tmpdir, monkeypatch, os_env, reactor_config_map):  # noqa
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version='1.0',
                                            release='1')
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)

        # No BUILD metadata
        monkeypatch.setenv("BUILD", json.dumps({}))
        with pytest.raises(PluginFailedException):
            runner.run()

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
    def test_koji_upload_krb_args(self, tmpdir, params, os_env, reactor_config_map):
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
                               keytab=params['keytab'],
                               reactor_config_map=reactor_config_map)

        if params['should_raise']:
            expectation.never()
            with pytest.raises(PluginFailedException):
                runner.run()
        else:
            expectation.once()
            runner.run()

    def test_koji_upload_krb_fail(self, tmpdir, os_env, reactor_config_map):  # noqa
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

    def test_koji_upload_ssl_fail(self, tmpdir, os_env, reactor_config_map):  # noqa
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
        runner = create_runner(tasker, workflow, ssl_certs=True,
                               reactor_config_map=reactor_config_map)
        with pytest.raises(PluginFailedException):
            runner.run()

    @pytest.mark.parametrize('fail_method', [
        'get_build_logs',
        'get_pod_for_build',
    ])
    def test_koji_upload_osbs_fail(self, tmpdir, os_env, fail_method, reactor_config_map):
        tasker, workflow = mock_environment(tmpdir,
                                            name='name',
                                            version='1.0',
                                            release='1')
        (flexmock(OSBS)
            .should_receive(fail_method)
            .and_raise(OsbsException))

        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

    def test_koji_upload_rpm_components(self, tmpdir, os_env, reactor_config_map):  # noqa
        session = MockedClientSession('')
        osbs = MockedOSBS()
        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            name='ns/name',
                                            version='1.0',
                                            release='1')
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()
        data = get_metadata(workflow, osbs)
        buildroots = data['buildroots']
        for buildroot in buildroots:
            assert any(c['name'] == 'name1' for c in buildroot['components'])
            assert any(c['version'] == '01234567' for c in buildroot['components'])
            assert any(c['arch'] == 'noarch' for c in buildroot['components'])

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
            'koji',
        ])

        assert is_string_type(osbs['build_id'])
        assert is_string_type(osbs['builder_image_id'])

        koji = osbs['koji']
        assert isinstance(koji, dict)
        assert set(koji.keys()) == set([
            'build_name',
            'builder_image_id',
        ])
        assert is_string_type(koji['build_name'])
        builder_image_id = koji['builder_image_id']
        assert isinstance(builder_image_id, dict)
        assert isinstance(builder_image_id, dict)
        for key in builder_image_id:
            assert is_string_type(builder_image_id[key])

    def validate_output(self, output, has_config,
                        base_from_scratch=False):
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
            assert output['arch'] == LOCAL_ARCH
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
                'layer_sizes',
                'tags',
            ])
            if base_from_scratch:
                expected_keys_set.remove('parent_id')
                assert 'parent_id' not in docker
            else:
                assert is_string_type(docker['parent_id'])

            if has_config:
                expected_keys_set.add('config')

            expected_keys_set.add('digests')
            assert set(docker.keys()) == expected_keys_set

            assert is_string_type(docker['id'])
            repositories = docker['repositories']
            assert isinstance(repositories, list)
            repositories_digest = list(filter(lambda repo: '@sha256' in repo, repositories))
            repositories_tag = list(filter(lambda repo: '@sha256' not in repo, repositories))

            assert len(repositories_tag) == 1
            assert len(repositories_digest) == 1

            # check for duplicates
            assert sorted(repositories_tag) == sorted(set(repositories_tag))
            assert sorted(repositories_digest) == sorted(set(repositories_digest))

            for repository in repositories_tag:
                assert is_string_type(repository)
                image = ImageName.parse(repository)
                assert image.registry
                assert image.namespace
                assert image.repo
                assert image.tag and image.tag != 'latest'

            digest_pullspec = image.to_str(tag=False) + '@' + fake_digest(image)
            assert digest_pullspec in repositories_digest
            digests = docker['digests']
            assert isinstance(digests, dict)

            tags = docker['tags']
            assert isinstance(tags, list)
            assert all(is_string_type(tag) for tag in tags)

            if has_config:
                config = docker['config']
                assert isinstance(config, dict)
                assert 'container_config' not in [x.lower() for x in config.keys()]
                assert all(is_string_type(entry) for entry in config)

    def test_koji_upload_import_fail(self, tmpdir, os_env, caplog, reactor_config_map):  # noqa
        session = MockedClientSession('')
        (flexmock(OSBS)
            .should_receive('create_config_map')
            .and_raise(OsbsException))
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

    @pytest.mark.parametrize('additional_tags', [
        None,
        ['3.2'],
    ])
    def test_koji_upload_image_tags(self, tmpdir, os_env, additional_tags, reactor_config_map):
        osbs = MockedOSBS()
        session = MockedClientSession('')
        version = '3.2.1'
        release = '4'
        tasker, workflow = mock_environment(tmpdir,
                                            name='ns/name',
                                            version=version,
                                            release=release,
                                            session=session,
                                            additional_tags=additional_tags)
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        data = get_metadata(workflow, osbs)

        # Find the docker output section
        outputs = data['output']
        docker_outputs = [output for output in outputs
                          if output['type'] == 'docker-image']
        assert len(docker_outputs) == 1
        output = docker_outputs[0]

        # Check the extra.docker.tags field
        docker = output['extra']['docker']
        assert isinstance(docker, dict)
        assert 'tags' in docker
        tags = docker['tags']
        assert isinstance(tags, list)
        expected_tags = set([version,
                             "{}-{}".format(version, release),
                             'latest',
                             "{}-timestamp".format(version)])
        if additional_tags:
            expected_tags.update(additional_tags)

        assert set(tags) == expected_tags

    @pytest.mark.parametrize(('blocksize'), [
        (None),
        (10485760),
    ])
    @pytest.mark.parametrize('has_config', (True, False))
    @pytest.mark.parametrize('base_from_scratch', (True, False))
    def test_koji_upload_success(self, tmpdir,
                                 blocksize,
                                 os_env, has_config,
                                 base_from_scratch, reactor_config_map):
        osbs = MockedOSBS()
        session = MockedClientSession('')
        component = 'component'
        name = 'ns/name'
        version = '1.0'
        release = '1'
        expected_build_name = 'ns/name:1.0-1'

        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            name=name,
                                            component=component,
                                            version=version,
                                            release=release,
                                            blocksize=blocksize,
                                            has_config=has_config,
                                            )
        workflow.builder.base_from_scratch = base_from_scratch
        target = 'images-docker-candidate'
        runner = create_runner(tasker, workflow, blocksize=blocksize, target=target,
                               platform=LOCAL_ARCH,
                               reactor_config_map=reactor_config_map)
        runner.run()

        data = get_metadata(workflow, osbs)

        assert set(data.keys()) == set([
            'metadata_version',
            'buildroots',
            'output',
        ])

        assert data['metadata_version'] in ['0', 0]

        buildroots = data['buildroots']
        assert isinstance(buildroots, list)
        assert len(buildroots) > 0

        output_files = data['output']
        assert isinstance(output_files, list)

        for buildroot in buildroots:
            self.validate_buildroot(buildroot)
            assert buildroot['extra']['osbs']['koji']['build_name'] == expected_build_name

            # Unique within buildroots in this metadata
            assert len([b for b in buildroots
                        if b['id'] == buildroot['id']]) == 1

        for output in output_files:
            self.validate_output(output, has_config,
                                 base_from_scratch=base_from_scratch)
            buildroot_id = output['buildroot_id']

            # References one of the buildroots
            assert len([buildroot for buildroot in buildroots
                        if buildroot['id'] == buildroot_id]) == 1

        files = session.uploaded_files

        # There should be a file in the list for each output
        assert isinstance(files, list)
        expected_uploads = len(output_files)

        assert len(files) == expected_uploads

        # The correct blocksize argument should have been used
        if blocksize is not None:
            assert blocksize == session.blocksize

    def test_koji_upload_pullspec(self, tmpdir, os_env, reactor_config_map):  # noqa
        osbs = MockedOSBS()
        session = MockedClientSession('')
        name = 'ns/name'
        version = '1.0'
        release = '1'
        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            name=name,
                                            version=version,
                                            release=release,
                                            )
        runner = create_runner(tasker, workflow, reactor_config_map=reactor_config_map)
        runner.run()

        metadata = get_metadata(workflow, osbs)
        docker_outputs = [
            output
            for output in metadata['output']
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

        tag_pullspecs = [
            repo
            for repo in docker_output['extra']['docker']['repositories']
            if '@sha256' not in repo
        ]
        assert len(tag_pullspecs) == 1
        pullspec = tag_pullspecs[0]

        nvr_tag = '{}:{}-{}'.format(name, version, release)
        assert pullspec.endswith(nvr_tag)

    @pytest.mark.parametrize('is_scratch', [
        True,
        False,
    ])
    @pytest.mark.parametrize('logs_return_bytes', [
        True,
        False,
    ])
    @pytest.mark.parametrize('platform,expected_logs', [
        (None, set(['x86_64-build.log'])),
        ('foo', set(['foo-build.log'])),
    ])
    def test_koji_upload_logs(self, tmpdir, monkeypatch, os_env, is_scratch, logs_return_bytes,
                              platform, expected_logs, reactor_config_map):
        MockedOSBS(logs_return_bytes=logs_return_bytes)
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            name='name',
                                            version='1.0',
                                            release='1')
        monkeypatch.setenv('BUILD', json.dumps({
            "metadata": {
                "creationTimestamp": "2015-07-27T09:24:00Z",
                "namespace": NAMESPACE,
                "name": BUILD_ID,
                "labels": {'scratch': is_scratch},
            }
        }))
        runner = create_runner(tasker, workflow, platform=platform,
                               reactor_config_map=reactor_config_map)
        runner.run()

        log_files = set(f for f in session.uploaded_files
                        if f.endswith('.log'))

        if is_scratch:
            expected_logs = set([])
        assert log_files == expected_logs

        images = [f for f in session.uploaded_files
                  if f not in log_files]
        if is_scratch:
            assert len(images) == 0
        else:
            assert len(images) == 1

        if platform is None:
            platform = 'x86_64'

        if not is_scratch:
            assert images[0].endswith(platform + ".tar.xz")

    @pytest.mark.parametrize('multiple', [False, True])
    def test_koji_upload_multiple_digests(self, tmpdir, os_env,
                                          multiple, reactor_config_map):
        server = MockedOSBS()
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            name='name',
                                            version='1.0',
                                            release='1')
        runner = create_runner(tasker, workflow, platform='x86_64',
                               multiple=multiple, reactor_config_map=reactor_config_map)
        workflow.plugin_workspace[ReactorConfigPlugin.key] = MockedReactorConfig()
        runner.run()

        data_list = list(server.configmap.values())
        if data_list:
            data = data_list[0]
        else:
            raise RuntimeError("no configmap found")

        outputs = data['metadata.json']['output']
        output = [op for op in outputs if op['type'] == 'docker-image'][0]
        repositories = output['extra']['docker']['repositories']
        pullspecs = [pullspec for pullspec in repositories
                     if '@' in pullspec]
        assert len(pullspecs) == 1

    @pytest.mark.parametrize('multiple', [False, True])
    def test_koji_upload_available_references(self, tmpdir, os_env,
                                              multiple, reactor_config_map):
        server = MockedOSBS()
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir,
                                            session=session,
                                            name='name',
                                            version='1.0',
                                            release='1')
        runner = create_runner(tasker, workflow, platform='x86_64',
                               multiple=multiple, reactor_config_map=reactor_config_map)
        workflow.plugin_workspace[ReactorConfigPlugin.key] = MockedReactorConfig()
        runner.run()

        data_list = list(server.configmap.values())
        if data_list:
            data = data_list[0]
        else:
            raise RuntimeError("no configmap found")

        outputs = data['metadata.json']['output']
        output = [op for op in outputs if op['type'] == 'docker-image'][0]
        repositories = output['extra']['docker']['repositories']
        digests = output['extra']['docker']['digests']
        expected = (1, 2)
        assert (len(digests), len(repositories)) == expected

    @pytest.mark.parametrize('has_operator_manifests', [False, True])
    def test_koji_upload_operator_manifests(self, tmpdir, monkeypatch, os_env,
                                            reactor_config_map, has_operator_manifests):
        server = MockedOSBS()
        session = MockedClientSession('')
        tasker, workflow = mock_environment(tmpdir, session=session,
                                            name='name', version='1.0', release='1')
        op_manifests_path = os.path.join(str(tmpdir), OPERATOR_MANIFESTS_ARCHIVE)
        with tempfile.NamedTemporaryFile() as stub:
            stub.write(b'stub')
            stub.flush()
            with zipfile.ZipFile(op_manifests_path, 'w') as archive:
                archive.write(stub.name, 'stub.yml')

        if has_operator_manifests:
            workflow.postbuild_results[PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY] = op_manifests_path
        runner = create_runner(tasker, workflow, platform='x86_64',
                               reactor_config_map=reactor_config_map)
        runner.run()
        data_list = list(server.configmap.values())
        if data_list:
            data = data_list[0]
        else:
            raise RuntimeError("no configmap found")

        if has_operator_manifests:
            outputs = data['metadata.json']['output']
            operator_output = [op for op in outputs if op['type'] == 'operator-manifests']
            assert len(operator_output) == 1
            assert operator_output[0]['filename'] == OPERATOR_MANIFESTS_ARCHIVE

            assert OPERATOR_MANIFESTS_ARCHIVE in session.uploaded_files
        else:
            assert OPERATOR_MANIFESTS_ARCHIVE not in session.uploaded_files
