"""
Copyright (c) 2016, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import re
from typing import Any, Dict

import koji
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugins.post_fetch_docker_archive import FetchDockerArchivePlugin
from atomic_reactor.util import DockerfileImages, ManifestDigest, RegistryClient
import atomic_reactor.utils.koji as koji_util

from osbs.repo_utils import ModuleSpec
from osbs.utils import ImageName
from atomic_reactor.utils.koji import (koji_login, create_koji_session,
                                       TaskWatcher, tag_koji_build,
                                       get_koji_module_build, KojiUploadLogger,
                                       get_output)
from atomic_reactor.plugin import BuildCanceledException
from atomic_reactor.constants import (KOJI_MAX_RETRIES,
                                      KOJI_OFFLINE_RETRY_INTERVAL,
                                      KOJI_RETRY_INTERVAL,
                                      OPERATOR_MANIFESTS_ARCHIVE,
                                      PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY,
                                      IMAGE_TYPE_DOCKER_ARCHIVE)
from flexmock import flexmock
import pytest


KOJI_RETRY_OPTS = {'anon_retry': True, 'max_retries': KOJI_MAX_RETRIES,
                   'retry_interval': KOJI_RETRY_INTERVAL, 'offline_retry': True,
                   'offline_retry_interval': KOJI_OFFLINE_RETRY_INTERVAL}


class TestKojiLogin(object):
    @pytest.mark.parametrize('proxyuser', [None, 'proxy'])
    def test_koji_login_krb_keyring(self, proxyuser):
        session = flexmock()
        expectation = session.should_receive('krb_login').once().and_return(True)
        kwargs = {}
        if proxyuser is not None:
            expectation.with_args(proxyuser=proxyuser)
            kwargs['proxyuser'] = proxyuser
        else:
            expectation.with_args()

        koji_login(session, **kwargs)

    @pytest.mark.parametrize('proxyuser', [None, 'proxy'])
    def test_koji_login_krb_keytab(self, proxyuser):
        session = flexmock()
        expectation = session.should_receive('krb_login').once().and_return(True)
        principal = 'user'
        keytab = '/keytab'
        call_kwargs = {
            'krb_principal': principal,
            'krb_keytab': keytab,
        }
        exp_kwargs = {
            'principal': principal,
            'keytab': keytab,
        }
        if proxyuser is not None:
            call_kwargs['proxyuser'] = proxyuser
            exp_kwargs['proxyuser'] = proxyuser

        expectation.with_args(**exp_kwargs)
        koji_login(session, **call_kwargs)

    @pytest.mark.parametrize('proxyuser', [None, 'proxy'])
    @pytest.mark.parametrize('serverca', [True, False])
    def test_koji_login_ssl(self, tmpdir, proxyuser, serverca):
        session = flexmock()
        expectation = session.should_receive('ssl_login').once().and_return(True)
        call_kwargs = {
            'ssl_certs_dir': str(tmpdir),
        }
        exp_kwargs = {
            'cert': str(tmpdir.join('cert')),
            'ca': None,
        }

        if serverca:
            serverca = tmpdir.join('serverca')
            serverca.write('spam')
            exp_kwargs['serverca'] = str(serverca)

        if proxyuser:
            call_kwargs['proxyuser'] = proxyuser
            exp_kwargs['proxyuser'] = proxyuser

        expectation.with_args(**exp_kwargs)
        koji_login(session, **call_kwargs)


class TestCreateKojiSession(object):
    def test_create_simple_session(self):
        url = 'https://example.com'
        session = flexmock()
        opts = {'krb_rdns': False, 'use_fast_upload': True}
        opts.update(KOJI_RETRY_OPTS)

        (flexmock(koji_util.koji).should_receive('ClientSession').with_args(
            url, opts=opts).and_return(session))
        assert create_koji_session(url) == session

    @pytest.mark.parametrize(('ssl_session'), [
        (True, False),
    ])
    def test_create_authenticated_session(self, tmpdir, ssl_session):
        url = 'https://example.com'
        args = {}

        session = flexmock()
        if ssl_session:
            args['ssl_certs_dir'] = str(tmpdir)
            session.should_receive('ssl_login').once().and_return(True)
        else:
            session.should_receive('krb_login').once().and_return(True)

        opts = {'krb_rdns': False, 'use_fast_upload': True}
        opts.update(KOJI_RETRY_OPTS)

        (flexmock(koji_util.koji).should_receive('ClientSession').with_args(
            url, opts=opts).and_return(session))
        assert create_koji_session(url, args) == session

    @pytest.mark.parametrize(('ssl_session'), [
        (True, False),
    ])
    def test_fail_authenticated_session(self, tmpdir, ssl_session):
        url = 'https://example.com'
        args = {}

        session = flexmock()
        if ssl_session:
            args['ssl_certs_dir'] = str(tmpdir)
            session.should_receive('ssl_login').once().and_return(False)
        else:
            session.should_receive('krb_login').once().and_return(False)

        opts = {'krb_rdns': False, 'use_fast_upload': True}
        opts.update(KOJI_RETRY_OPTS)

        (flexmock(koji_util.koji).should_receive('ClientSession').with_args(
            url, opts=opts).and_return(session))
        with pytest.raises(RuntimeError):
            create_koji_session(url, args)


class TestStreamTaskOutput(object):
    def test_output_as_generator(self):
        contents = 'this is the simulated file contents'

        session = flexmock()
        expectation = session.should_receive('downloadTaskOutput')

        for chunk in contents:
            expectation = expectation.and_return(chunk)
        # Empty content to simulate end of stream.
        expectation.and_return('')

        streamer = koji_util.stream_task_output(session, 123, 'file.ext')
        assert ''.join(list(streamer)) == contents


class TestTaskWatcher(object):
    @pytest.mark.parametrize(('finished', 'info', 'exp_state', 'exp_failed'), [
        ([False, False, True],
         {'state': koji.TASK_STATES['CANCELED']},
         'CANCELED', True),

        ([False, True],
         {'state': koji.TASK_STATES['FAILED']},
         'FAILED', True),

        ([True],
         {'state': koji.TASK_STATES['CLOSED']},
         'CLOSED', False),
    ])
    def test_wait(self, finished, info, exp_state, exp_failed):
        session = flexmock()
        task_id = 1234
        task_finished = (session.should_receive('taskFinished')
                         .with_args(task_id))
        for finished_value in finished:
            task_finished = task_finished.and_return(finished_value)

        (session.should_receive('getTaskInfo')
            .with_args(task_id, request=True)
            .once()
            .and_return(info))

        task = TaskWatcher(session, task_id, poll_interval=0)
        assert task.wait() == exp_state
        assert task.failed() == exp_failed

    def test_cancel(self):
        session = flexmock()
        task_id = 1234
        (session
            .should_receive('taskFinished')
            .with_args(task_id)
            .and_raise(BuildCanceledException))

        task = TaskWatcher(session, task_id, poll_interval=0)
        with pytest.raises(BuildCanceledException):
            task.wait()

        assert task.failed()


class TestTagKojiBuild(object):
    @pytest.mark.parametrize(('task_state', 'failure'), (
        ('CLOSED', False),
        ('CANCELED', True),
        ('FAILED', True),
    ))
    def test_tagging(self, task_state, failure):
        session = flexmock()
        task_id = 9876
        build_id = 1234
        target_name = 'target'
        tag_name = 'images-candidate'
        target_info = {'dest_tag_name': tag_name}
        task_info = {'state': koji.TASK_STATES[task_state]}

        (session
            .should_receive('getBuildTarget')
            .with_args(target_name)
            .and_return(target_info))
        (session
            .should_receive('tagBuild')
            .with_args(tag_name, build_id)
            .and_return(task_id))
        (session
            .should_receive('taskFinished')
            .with_args(task_id)
            .and_return(True))
        (session
            .should_receive('getTaskInfo')
            .with_args(task_id, request=True)
            .and_return(task_info))

        if failure:
            with pytest.raises(RuntimeError):
                tag_koji_build(session, build_id, target_name)
        else:
            build_tag = tag_koji_build(session, build_id, target_name)
            assert build_tag == tag_name


class TestGetKojiModuleBuild(object):
    def mock_get_rpms(self, session):
        (session
            .should_receive('listArchives')
            .with_args(buildID=1138198)
            .once()
            .and_return(
                [{'btype': 'module',
                  'build_id': 1138198,
                  'filename': 'modulemd.txt',
                  'id': 147879},
                 {'btype': 'module',
                  'build_id': 1138198,
                  'filename': 'modulemd.x86_64.txt',
                  'id': 147880}]))
        (session
            .should_receive('listRPMs')
            .with_args(imageID=147879)
            .once()
            .and_return([
                {'arch': 'src',
                 'epoch': None,
                 'id': 15197182,
                 'name': 'eog',
                 'release': '1.module_2123+73a9ef6f',
                 'version': '3.28.3'},
                {'arch': 'x86_64',
                 'epoch': None,
                 'id': 15197187,
                 'metadata_only': False,
                 'name': 'eog',
                 'release': '1.module_2123+73a9ef6f',
                 'version': '3.28.3'},
                {'arch': 'ppc64le',
                 'epoch': None,
                 'id': 15197188,
                 'metadata_only': False,
                 'name': 'eog',
                 'release': '1.module_2123+73a9ef6f',
                 'version': '3.28.3'},
             ]))

    def test_with_context(self):
        module = 'eog:my-stream:20180821163756:775baa8e'
        module_koji_nvr = 'eog-my_stream-20180821163756.775baa8e'
        koji_return = {
            'build_id': 1138198,
            'name': 'eog',
            'version': 'my_stream',
            'release': '20180821163756.775baa8e',
            'extra': {
                'typeinfo': {
                    'module': {
                        'modulemd_str': 'document: modulemd\nversion: 2'
                    }
                }
            }
        }

        spec = ModuleSpec.from_str(module)
        session = flexmock()
        (session
            .should_receive('getBuild')
            .with_args(module_koji_nvr)
            .and_return(koji_return))
        self.mock_get_rpms(session)

        get_koji_module_build(session, spec)

    # CLOUDBLD-876
    def test_with_context_without_build(self):
        module = 'eog:my-stream:20180821163756:775baa8e'
        module_koji_nvr = 'eog-my_stream-20180821163756.775baa8e'
        koji_return = None

        spec = ModuleSpec.from_str(module)
        session = flexmock()
        (session
            .should_receive('getBuild')
            .with_args(module_koji_nvr)
            .and_return(koji_return))

        with pytest.raises(Exception) as e:
            get_koji_module_build(session, spec)
        assert 'No build found' in str(e.value)

    @pytest.mark.parametrize(('koji_return', 'should_raise'), [
        ([{
            'build_id': 1138198,
            'name': 'eog',
            'version': 'master',
            'release': '20180821163756.775baa8e',
            'extra': {
                'typeinfo': {
                    'module': {
                        'modulemd_str': 'document: modulemd\nversion: 2'
                    }
                }
            }
        }], None),
        ([], "No build found for"),
        ([{
            'build_id': 1138198,
            'name': 'eog',
            'version': 'master',
            'release': '20180821163756.775baa8e',
          },
          {
            'build_id': 1138199,
            'name': 'eog',
            'version': 'master',
            'release': '20180821163756.88888888',
          }],
         "Multiple builds found for"),
    ])
    def test_without_context(self, koji_return, should_raise):
        module = 'eog:master:20180821163756'
        spec = ModuleSpec.from_str(module)

        session = flexmock()
        (session
            .should_receive('getPackageID')
            .with_args('eog')
            .and_return(303))
        (session
            .should_receive('listBuilds')
            .with_args(packageID=303,
                       type='module',
                       state=koji.BUILD_STATES['COMPLETE'])
            .and_return(koji_return))

        if should_raise:
            with pytest.raises(Exception) as e:
                get_koji_module_build(session, spec)
            assert should_raise in str(e.value)
        else:
            self.mock_get_rpms(session)
            get_koji_module_build(session, spec)


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


# Test whether extra.docker.parent_id should be set
@pytest.mark.parametrize('from_scratch', [True, False])
@pytest.mark.parametrize('no_v2_digest', [True, False])
@pytest.mark.parametrize('has_export_operator_manifests', [True, False])
def test_binary_build_get_output(has_export_operator_manifests: bool,
                                 no_v2_digest: bool,
                                 from_scratch: bool,
                                 workflow: DockerBuildWorkflow,
                                 tmpdir):
    platform = "x86_64"

    if from_scratch:
        workflow.data.dockerfile_images = DockerfileImages(['scratch'])
        parent_id = None
    else:
        workflow.data.dockerfile_images = DockerfileImages(['fedora:35'])
        parent_id = 'parent-id'
        (flexmock(workflow.imageutil)
         .should_receive('base_image_inspect')
         .with_args(platform)
         .and_return({'Id': parent_id}))

    # For verifying the tags in final metadata
    primary_image = ImageName.parse("ns/image:1-2")
    workflow.data.tag_conf.add_primary_image(primary_image)
    unique_image = ImageName.parse("ns/image:candidate-202203291618")
    workflow.data.tag_conf.add_unique_image(unique_image)

    # Mock for ImageUtil.get_uncompressed_layer_sizes
    layer_sizes = [
        {"diff_id": 1, "size": 100},
        {"diff_id": 2, "size": 200},
    ]
    workflow.build_dir.init_build_dirs([platform], workflow.source)
    platform_dir = workflow.build_dir.platform_dir(platform)
    (flexmock(workflow.imageutil)
     .should_receive('get_uncompressed_image_layer_sizes')
     .with_args(str(platform_dir.exported_squashed_image))
     .and_return(layer_sizes))

    workflow.conf.conf = {
        'registries': [
            {'url': 'https://registry.host/', 'insecure': False},
        ],
    }
    # Mock get manifest digests
    # What would happen if there is no both v2 and oci digest?
    image_manifest_digest = ManifestDigest(
        {'oci': 'oci-1234'} if no_v2_digest else {'v2': '1234'}
    )
    (flexmock(RegistryClient)
     .should_receive('get_manifest_digests')
     .and_return(image_manifest_digest))
    # Mock getting image config
    blob_config = {'oci': 'oci-1234'} if no_v2_digest else {'v2': '1234'}
    (flexmock(RegistryClient)
     .should_receive('get_config_and_id_from_registry')
     .and_return((blob_config, None)))

    # Assume FetchDockerArchivePlugin has run and metadata of the
    # platform-specific built image archive has been saved.
    workflow.data.postbuild_results[FetchDockerArchivePlugin.key] = {
        platform: {'type': IMAGE_TYPE_DOCKER_ARCHIVE}
    }

    if has_export_operator_manifests:
        archive_file = tmpdir.join(OPERATOR_MANIFESTS_ARCHIVE)
        archive_file.write_binary(b'20220329')
        workflow.data.postbuild_results[PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY] = str(archive_file)

    buildroot_id = f'{platform}-1'
    image_pullspec = ImageName.parse("ns/image:latest")
    output, output_file = get_output(
        workflow, buildroot_id, image_pullspec, platform, source_build=False
    )

    # Prepare expected metadata

    expected_repositories = sorted([
        # Pull image with a specific tag
        image_pullspec.to_str(),
        # Pull image with a specific digest
        f'{image_pullspec.to_str(tag=False)}@{image_manifest_digest.oci}'
        if no_v2_digest else
        f'{image_pullspec.to_str(tag=False)}@{image_manifest_digest.v2}',
    ])
    expected_metadata: Dict[str, Any] = {
        'buildroot_id': buildroot_id,
        'checksum_type': 'md5',
        'arch': platform,
        'type': 'docker-image',
        'components': [],
        'extra': {
            'image': {'arch': platform},
            'docker': {
                'id': None,
                'repositories': expected_repositories,
                'layer_sizes': layer_sizes,
                'tags': sorted([primary_image.tag, unique_image.tag]),
                'config': blob_config,
                'digests': None,  # Set later below
            },
        },
    }

    extra_docker = expected_metadata['extra']['docker']
    if not from_scratch:
        extra_docker['parent_id'] = parent_id

    extra_docker['digests'] = (
        {ManifestDigest.content_type['oci']: image_manifest_digest.oci}
        if no_v2_digest else
        {ManifestDigest.content_type['v2']: image_manifest_digest.v2}
    )

    # Start assertions
    assert output_file is None
    if has_export_operator_manifests:
        assert len(output) == 2
    else:
        assert len(output) == 1

    image_metadata = output[0].metadata

    # Assert these image metadata firstly, then remove them and assert the
    # rest. So, no need to mock anything for get_image_output.
    assert 'docker-image-None.x86_64.tar.gz' == image_metadata.pop('filename')
    assert image_metadata.pop('filesize') > 0
    assert re.match(r'^[0-9a-f]+$', image_metadata.pop('checksum'))

    # Make it easier for comparison below
    extra_docker = image_metadata['extra']['docker']
    extra_docker['repositories'] = sorted(extra_docker['repositories'])
    extra_docker['tags'] = sorted(extra_docker['tags'])

    assert expected_metadata == image_metadata

    if has_export_operator_manifests:
        manifests_output = output[1]
        assert str(tmpdir.join(OPERATOR_MANIFESTS_ARCHIVE)) == manifests_output.filename
        assert buildroot_id == manifests_output.metadata['buildroot_id']
        assert OPERATOR_MANIFESTS_ARCHIVE == manifests_output.metadata['filename']
