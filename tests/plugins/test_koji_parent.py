"""
Copyright (c) 2017, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import json
import koji
from pathlib import Path

import atomic_reactor
from atomic_reactor.constants import (
    DOCKERFILE_FILENAME, INSPECT_CONFIG, BASE_IMAGE_KOJI_BUILD, PARENT_IMAGES_KOJI_BUILDS,
    PLUGIN_CHECK_AND_SET_PLATFORMS_KEY
)
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_koji_parent import KojiParentPlugin
from atomic_reactor.util import get_manifest_media_type, DockerfileImages
from osbs.utils import ImageName
from flexmock import flexmock
from tests.util import add_koji_map_in_workflow
from copy import deepcopy

import pytest


KOJI_HUB = 'http://koji.com/hub'

KOJI_BUILD_ID = 123456789

KOJI_BUILD_NVR = 'base-image-1.0-99'

KOJI_STATE_COMPLETE = koji.BUILD_STATES['COMPLETE']
KOJI_STATE_BUILDING = koji.BUILD_STATES['BUILDING']

V2_LIST = get_manifest_media_type('v2_list')
V2 = get_manifest_media_type('v2')
KOJI_EXTRA = {'image': {'index': {'digests': {V2_LIST: 'stubDigest'}}}}

KOJI_STATE_DELETED = koji.BUILD_STATES['DELETED']

KOJI_BUILD_BUILDING = {'nvr': KOJI_BUILD_NVR, 'id': KOJI_BUILD_ID, 'state': KOJI_STATE_BUILDING,
                       'extra': KOJI_EXTRA}

KOJI_BUILD = {'nvr': KOJI_BUILD_NVR, 'id': KOJI_BUILD_ID, 'state': KOJI_STATE_COMPLETE,
              'extra': KOJI_EXTRA}

DELETED_KOJI_BUILD = {'nvr': KOJI_BUILD_NVR, 'id': KOJI_BUILD_ID, 'state': KOJI_STATE_DELETED}

BASE_IMAGE_LABELS = {
    'com.redhat.component': 'base-image',
    'version': '1.0',
    'release': '99',
}

BASE_IMAGE_LABELS_W_ALIASES = {
    'com.redhat.component': 'base-image',
    'BZComponent': 'base-image',
    'version': '1.0',
    'Version': '1.0',
    'release': '99',
    'Release': '99',
}


class MockSource(object):
    def __init__(self, source_dir: Path):
        self.dockerfile_path = str(source_dir / DOCKERFILE_FILENAME)
        self.path = str(source_dir)
        self.commit_id = None
        self.config = None


@pytest.fixture()
def workflow(workflow, source_dir):
    workflow.source = MockSource(source_dir)
    workflow.data.dockerfile_images = DockerfileImages(['base:latest'])
    workflow.data.dockerfile_images['base:latest'] = ImageName.parse('base:stubDigest')
    base_inspect = {INSPECT_CONFIG: {'Labels': BASE_IMAGE_LABELS.copy()}}
    flexmock(workflow.imageutil).should_receive('base_image_inspect').and_return(base_inspect)
    flexmock(workflow.imageutil).should_receive('get_inspect_for_image').and_return(base_inspect)
    workflow.data.parent_images_digests = {'base:latest': {V2_LIST: 'stubDigest'}}
    return workflow


@pytest.fixture()
def koji_session():
    session = flexmock()
    flexmock(session).should_receive('getBuild').with_args(KOJI_BUILD_NVR).and_return(KOJI_BUILD)
    flexmock(session).should_receive('krb_login').and_return(True)
    flexmock(koji).should_receive('ClientSession').and_return(session)
    return session


class TestKojiParent(object):

    def test_koji_build_found(self, workflow, koji_session):  # noqa
        self.run_plugin_with_args(workflow)

    @pytest.mark.skip(reason="Raising for manifests_mismatches is disabled")
    def test_koji_build_no_extra(self, workflow, koji_session):  # noqa
        koji_no_extra = {'nvr': KOJI_BUILD_NVR, 'id': KOJI_BUILD_ID, 'state': KOJI_STATE_COMPLETE}
        (flexmock(koji_session)
            .should_receive('getBuild')
            .with_args(KOJI_BUILD_NVR)
            .and_return(koji_no_extra))
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow)
        assert 'does not have manifest digest data' in str(exc_info.value)

    def test_koji_build_retry(self, workflow, koji_session):  # noqa
        (flexmock(koji_session)
            .should_receive('getBuild')
            .with_args(KOJI_BUILD_NVR)
            .and_return(None)
            .and_return(None)
            .and_return(None)
            .and_return(KOJI_BUILD_BUILDING)
            .and_return(KOJI_BUILD)
            .times(5))

        self.run_plugin_with_args(workflow)

    def test_koji_ssl_certs_used(self, tmpdir, workflow, koji_session):  # noqa
        serverca = tmpdir.join('serverca')
        serverca.write('spam')
        expected_ssl_login_args = {
            'cert': str(tmpdir.join('cert')),
            'serverca': str(serverca),
            'ca': None,
        }
        (flexmock(koji_session)
            .should_receive('ssl_login')
            .with_args(**expected_ssl_login_args)
            .and_return(True)
            .once())
        plugin_args = {'koji_ssl_certs_dir': str(tmpdir)}
        self.run_plugin_with_args(workflow, plugin_args)

    def test_koji_build_not_found(self, workflow, koji_session):  # noqa
        (flexmock(koji_session)
            .should_receive('getBuild')
            .with_args(KOJI_BUILD_NVR)
            .and_return(None))

        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow, {'poll_timeout': 0.01})
        assert 'KojiParentBuildMissing' in str(exc_info.value)

    def test_koji_build_deleted(self, workflow, koji_session):  # noqa
        (flexmock(koji_session)
            .should_receive('getBuild')
            .with_args(KOJI_BUILD_NVR)
            .and_return(DELETED_KOJI_BUILD))

        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow)
        assert 'KojiParentBuildMissing' in str(exc_info.value)
        assert 'state is DELETED, not COMPLETE' in str(exc_info.value)

    def test_base_image_not_inspected(self, workflow, koji_session):  # noqa
        flexmock(workflow.imageutil).should_receive('base_image_inspect').and_return({})
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow)
        assert 'KeyError' in str(exc_info.value)
        assert 'Config' in str(exc_info.value)

    @pytest.mark.parametrize('external', [True, False])
    @pytest.mark.parametrize(('remove_labels', 'exp_result'), [  # noqa: F811
        (['com.redhat.component'], True),
        (['BZComponent'], True),
        (['com.redhat.component', 'BZComponent'], False),
        (['version'], True),
        (['Version'], True),
        (['version', 'Version'], False),
        (['release'], True),
        (['Release'], True),
        (['release', 'Release'], False),
    ])
    def test_base_image_missing_labels(self, workflow, koji_session, remove_labels,
                                       exp_result, external, caplog):
        base_tag = ImageName.parse('base:stubDigest')

        base_inspect = {INSPECT_CONFIG: {'Labels': BASE_IMAGE_LABELS_W_ALIASES.copy()}}
        parent_inspect = {INSPECT_CONFIG: {'Labels': BASE_IMAGE_LABELS_W_ALIASES.copy()}}

        for label in remove_labels:
            del base_inspect[INSPECT_CONFIG]['Labels'][label]
            del parent_inspect[INSPECT_CONFIG]['Labels'][label]

        flexmock(workflow.imageutil).should_receive('base_image_inspect').and_return(base_inspect)
        (flexmock(workflow.imageutil)
         .should_receive('get_inspect_for_image')
         .with_args(base_tag)
         .and_return(parent_inspect))

        if not exp_result:
            if not external:
                with pytest.raises(PluginFailedException) as exc:
                    self.run_plugin_with_args(workflow, expect_result=exp_result,
                                              external_base=external)
                assert 'Was this image built in OSBS?' in str(exc.value)
            else:
                result = {PARENT_IMAGES_KOJI_BUILDS: {ImageName.parse('base'): None}}
                self.run_plugin_with_args(workflow, expect_result=result,
                                          external_base=external)
                assert 'Was this image built in OSBS?' in caplog.text
        else:
            self.run_plugin_with_args(workflow, expect_result=exp_result)

    @pytest.mark.parametrize('media_version', ['v2_list', 'v2'])
    @pytest.mark.parametrize('koji_mtype', [True, False])
    @pytest.mark.parametrize('parent_tags', [
                             ['miss', 'stubDigest', 'stubDigest'],
                             ['stubDigest', 'miss', 'stubDigest'],
                             ['stubDigest', 'stubDigest', 'miss'],
                             ['miss', 'miss', 'miss'],
                             ['stubDigest', 'stubDigest', 'stubDigest']])
    @pytest.mark.parametrize('special_base', [False, 'scratch', 'custom'])  # noqa: F811
    def test_multiple_parent_images(self, workflow, koji_session, koji_mtype,
                                    special_base, parent_tags, media_version, caplog):
        parent_images = {
            ImageName.parse('somebuilder'): ImageName.parse('somebuilder:{}'
                                                            .format(parent_tags[0])),
            ImageName.parse('otherbuilder'): ImageName.parse('otherbuilder:{}'
                                                             .format(parent_tags[1])),
            ImageName.parse('base'): ImageName.parse('base:{}'.format(parent_tags[2])),
        }
        media_type = get_manifest_media_type(media_version)
        workflow.data.parent_images_digests = {}
        for name, image in parent_images.items():
            workflow.data.parent_images_digests[name.to_str()] = {media_type: image.tag}
        if not koji_mtype:
            media_type = get_manifest_media_type('v1')
        extra = {'image': {'index': {'digests': {media_type: 'stubDigest'}}}}
        koji_builds = dict(
            somebuilder=dict(nvr='somebuilder-1.0-1',
                             id=42,
                             state=KOJI_STATE_COMPLETE,
                             extra=extra),
            otherbuilder=dict(nvr='otherbuilder-2.0-1',
                              id=43,
                              state=KOJI_STATE_COMPLETE,
                              extra=extra),
            base=dict(nvr=KOJI_BUILD_NVR, id=KOJI_BUILD_ID, state=KOJI_STATE_COMPLETE, extra=extra),
            unresolved=None,
        )
        image_inspects = {}
        koji_expects = {}

        # need to load up our mock objects with expected responses for the parents
        for img, build in koji_builds.items():
            if build is None:
                continue
            name, version, release = koji_builds[img]['nvr'].rsplit('-', 2)
            labels = {'com.redhat.component': name, 'version': version, 'release': release}
            image_inspects[img] = {INSPECT_CONFIG: dict(Labels=labels)}

            (flexmock(workflow.imageutil)
             .should_receive('get_inspect_for_image')
             .with_args(parent_images[ImageName.parse(img)])
             .and_return(image_inspects[img]))

            (koji_session.should_receive('getBuild')
                .with_args(koji_builds[img]['nvr'])
                .and_return(koji_builds[img]))
            koji_expects[ImageName.parse(img)] = build

        dockerfile_images = []
        for parent in parent_images:
            dockerfile_images.append(parent.to_str())

        if special_base == 'scratch':
            dockerfile_images.append('scratch')
        elif special_base == 'custom':
            dockerfile_images.append('koji/image-build')
        else:
            (flexmock(workflow.imageutil)
             .should_receive('base_image_inspect')
             .and_return(image_inspects['base']))

        workflow.data.dockerfile_images = DockerfileImages(dockerfile_images)
        for parent, local in parent_images.items():
            workflow.data.dockerfile_images[parent] = local

        expected = {
            BASE_IMAGE_KOJI_BUILD: koji_builds['base'],
            PARENT_IMAGES_KOJI_BUILDS: koji_expects,
        }
        if special_base:
            del expected[BASE_IMAGE_KOJI_BUILD]

        if not koji_mtype:
            self.run_plugin_with_args(
                workflow, expect_result=expected
            )
            assert 'does not have manifest digest data for the expected media type' in caplog.text
        elif 'miss' in parent_tags or not koji_mtype:
            self.run_plugin_with_args(
                workflow, expect_result=expected
            )
            errors = []
            error_msg = ('Manifest digest (miss) for parent image {}:latest does not match value '
                         'in its koji reference (stubDigest)')
            if parent_tags[0] == 'miss':
                errors.append(error_msg.format('somebuilder'))
            if parent_tags[1] == 'miss':
                errors.append(error_msg.format('otherbuilder'))
            if parent_tags[2] == 'miss':
                errors.append(error_msg.format('base'))
            assert 'This parent image MUST be rebuilt' in caplog.text
            for e in errors:
                assert e in caplog.text

        else:
            self.run_plugin_with_args(
                workflow, expect_result=expected
            )

    def test_unexpected_digest_data(self, workflow, koji_session):  # noqa
        workflow.data.parent_images_digests = {'base:latest': {'unexpected_type': 'stubDigest'}}
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow)
        assert 'Unexpected parent image digest data' in str(exc_info.value)

    @pytest.mark.parametrize(('source_registry', 'pull_registries'), [
        (True, False),
        (False, True),
    ])
    @pytest.mark.parametrize('feature_flag', [True, False])
    @pytest.mark.parametrize('parent_tag', ['stubDigest', 'wrongDigest'])
    @pytest.mark.parametrize('has_registry', [True, False])
    @pytest.mark.parametrize('mismatch_failure', [True, False])
    @pytest.mark.parametrize('manifest_list', [
        {'manifests': [
            {'digest': 'stubDigest', 'mediaType': V2, 'platform': {
                'architecture': 'amd64'}}]},
        {'manifests':
            [{'digest': 'differentDigest', 'mediaType': V2, 'platform': {
                'architecture': 'amd64'}}]},
        {'manifests':
            [{'digest': 'stubDigest', 'mediaType': 'unexpected', 'platform': {
                'architecture': 'amd64'}}]},
        {}])
    def test_deep_digest_inspection(self, workflow, koji_session, source_registry, pull_registries,
                                    feature_flag, parent_tag, caplog, has_registry, manifest_list,
                                    mismatch_failure):  # noqa
        image_str = 'base'
        registry = 'examples.com'
        if has_registry:
            image_str = '/'.join([registry, image_str])
        extra = {'image': {'index': {'digests': {V2_LIST: 'stubDigest'}}}}

        workflow.data.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = ['x86_64']

        koji_build = dict(nvr='base-image-1.0-99',
                          id=KOJI_BUILD_ID,
                          state=KOJI_STATE_COMPLETE,
                          extra=extra)
        (koji_session.should_receive('getBuild')
            .and_return(koji_build))
        archives = [{
            'btype': 'image',
            'extra': {
                'docker': {
                    'config': {
                        'architecture': 'amd64'
                        },
                    'digests': {
                        V2: 'stubDigest'}}}}]
        (koji_session.should_receive('listArchives')
            .and_return(archives))

        name, version, release = koji_build['nvr'].rsplit('-', 2)
        labels = {'com.redhat.component': name, 'version': version, 'release': release}
        image_inspect = {INSPECT_CONFIG: dict(Labels=labels)}

        (flexmock(workflow.imageutil)
         .should_receive('get_inspect_for_image')
         .and_return(image_inspect))
        if manifest_list:
            response = flexmock(content=json.dumps(manifest_list))
        else:
            response = {}
        (flexmock(atomic_reactor.util.RegistryClient)
         .should_receive('get_manifest')
         .and_return((response, None)))

        expected_result = {BASE_IMAGE_KOJI_BUILD: KOJI_BUILD,
                           PARENT_IMAGES_KOJI_BUILDS: {
                               ImageName.parse(image_str): KOJI_BUILD}}

        workflow.data.parent_images_digests = {image_str+':latest': {V2_LIST: parent_tag}}
        workflow.data.dockerfile_images = DockerfileImages([image_str])
        workflow.data.dockerfile_images[image_str] = ImageName.parse(f'{image_str}:{parent_tag}')

        rebuild_str = 'This parent image MUST be rebuilt'
        manifest_list_check_passed = ('Deeper manifest list check verified v2 manifest '
                                      'references match')

        defective_v2 = (not has_registry
                        or manifest_list.get('manifests', [{}])[0].get('digest') != 'stubDigest'
                        or manifest_list['manifests'][0]['mediaType'] != V2)

        pull_r = [{'url': registry}] if pull_registries else None
        source_r = {'url': registry} if source_registry else None
        if (mismatch_failure and parent_tag != 'stubDigest' and
           (not feature_flag or defective_v2)):
            with pytest.raises(PluginFailedException) as exc_info:
                self.run_plugin_with_args(workflow,
                                          expect_result=expected_result,
                                          deep_inspection=feature_flag,
                                          mismatch_failure=mismatch_failure,
                                          pull_registries=pull_r,
                                          source_registry=source_r)
            assert rebuild_str in str(exc_info.value)
        else:
            self.run_plugin_with_args(workflow, expect_result=expected_result,
                                      deep_inspection=feature_flag,
                                      pull_registries=pull_r,
                                      source_registry=source_r)

        if parent_tag == 'stubDigest':
            assert rebuild_str not in caplog.text
        else:
            if not feature_flag:
                assert 'Checking manifest list contents' not in caplog.text
                assert rebuild_str in caplog.text
            else:
                assert 'Checking manifest list contents' in caplog.text
                if has_registry and manifest_list:
                    if manifest_list['manifests'][0]['mediaType'] != V2:
                        assert 'Unexpected media type in manifest list' in caplog.text
                        assert rebuild_str in caplog.text
                    elif manifest_list['manifests'][0]['digest'] == 'stubDigest':
                        assert rebuild_str not in caplog.text
                        assert manifest_list_check_passed in caplog.text
                    else:
                        assert 'differs from Koji archive digest' in caplog.text
                        assert rebuild_str in caplog.text
                else:
                    fetch_error = 'Could not fetch manifest list for {}:latest'.format(image_str)
                    assert fetch_error in caplog.text
                    assert rebuild_str in caplog.text

    def test_skip_build(self, workflow, caplog, koji_session):
        user_params = {'scratch': True}
        self.run_plugin_with_args(workflow, {'poll_timeout': 0.01},
                                  user_params=user_params)

        assert 'scratch build, skipping plugin' in caplog.text

    def run_plugin_with_args(self, workflow, plugin_args=None, expect_result=True,  # noqa
                             external_base=False, deep_inspection=True, mismatch_failure=False,
                             user_params=None, is_isolated=None,
                             pull_registries=None, source_registry=None):
        plugin_args = plugin_args or {}
        user_params = user_params or {}
        plugin_args.setdefault('poll_interval', 0.01)
        plugin_args.setdefault('poll_timeout', 1)
        workflow.user_params = user_params

        config_dict = {'version': 1,
                       'deep_manifest_list_inspection': deep_inspection,
                       'fail_on_digest_mismatch': mismatch_failure,
                       'skip_koji_check_for_base_image': external_base,
                       'platform_descriptors': [{'architecture': 'amd64', 'platform': 'x86_64'}]}

        if pull_registries:
            config_dict['pull_registries'] = pull_registries
        if source_registry:
            config_dict['source_registry'] = source_registry

        workflow.conf.conf = config_dict
        add_koji_map_in_workflow(workflow, hub_url=KOJI_HUB, root_url='',
                                 ssl_certs_dir=plugin_args.get('koji_ssl_certs_dir'))

        runner = PreBuildPluginsRunner(
            workflow,
            [{'name': KojiParentPlugin.key, 'args': plugin_args}]
        )

        result = runner.run()
        if user_params:
            return
        base_img = ImageName.parse('base:latest')
        if expect_result is True:
            expected_result = {BASE_IMAGE_KOJI_BUILD: KOJI_BUILD,
                               PARENT_IMAGES_KOJI_BUILDS: {
                                   base_img: KOJI_BUILD}}
            if is_isolated is not None:
                expected_result = deepcopy(expected_result)
                expected_result[BASE_IMAGE_KOJI_BUILD]['extra']['image']['isolated'] = is_isolated
                expected_result[PARENT_IMAGES_KOJI_BUILDS][base_img]['extra']['image']['isolated']\
                    = is_isolated
        elif expect_result is False:
            expected_result = None
        else:  # param provided the expected result
            expected_result = expect_result

        assert result[KojiParentPlugin.key] == expected_result

    @pytest.mark.parametrize(
        ('manifest_list', 'requested_platforms', 'expected_logs', 'not_expected_logs'),
        [
            # Test for requested arch by user which is not in Koji archive
            (
                    {'manifests': [
                        {'digest': 'stubDigest', 'mediaType': V2, 'platform': {
                            'architecture': 'amd64'}}]},
                    ['aarch64'],
                    ['Architectures "%s" are missing in Koji archives' % ['aarch64']],
                    []
            ),
            # Additional platforms might contain v2 digests of different builds
            # Test if ppc64le and s390x will be ignored
            (
                    {'manifests': [
                        {'digest': 'stubDigest', 'mediaType':
                            'application/vnd.docker.distribution.manifest.v2+json',
                         'platform': {'architecture': 'amd64'}},
                        {'digest': 'stubDigest2', 'mediaType':
                            'application/vnd.docker.distribution.manifest.v2+json',
                         'platform': {'architecture': 'ppc64le'}},
                        {'digest': 'stubDigest3', 'mediaType':
                            'application/vnd.docker.distribution.manifest.v2+json',
                         'platform': {'architecture': 's390x'}}]},
                    ['x86_64'],
                    ['Deeper manifest list check verified v2 manifest references match'],
                    ['This parent image MUST be rebuilt']
            ),
            # Test if ppc64le and s390x will be ignored and digest check will fail for amd64
            # because stubDigest is expected
            (
                    {'manifests': [
                        {'digest': 'notExpectedStubDigest', 'mediaType':
                            'application/vnd.docker.distribution.manifest.v2+json',
                         'platform': {'architecture': 'amd64'}},
                        {'digest': 'stubDigest2', 'mediaType':
                            'application/vnd.docker.distribution.manifest.v2+json',
                         'platform': {'architecture': 'ppc64le'}},
                        {'digest': 'stubDigest3', 'mediaType':
                            'application/vnd.docker.distribution.manifest.v2+json',
                         'platform': {'architecture': 's390x'}}]},
                    ['x86_64'],
                    [('parent image example.com/base:latest differs from the manifest list '
                      'for its koji reference')],
                    []
            )
        ])
    def test_deep_digests_with_requested_arches(self, workflow, koji_session, caplog,
                                                manifest_list, requested_platforms, expected_logs,
                                                not_expected_logs):  # noqa
        registry = 'example.com'
        image_str = '{}/base:latest'.format(registry)
        extra = {'image': {'index': {'digests': {V2_LIST: 'stubDigest'}}}}
        parent_tag = 'notExpectedDigest'
        workflow.data.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = requested_platforms

        koji_build = dict(nvr='base-image-1.0-99',
                          id=KOJI_BUILD_ID,
                          state=KOJI_STATE_COMPLETE,
                          extra=extra)
        (koji_session.should_receive('getBuild')
         .and_return(koji_build))
        archives = [{
            'btype': 'image',
            'extra': {
                'docker': {
                    'config': {
                        'architecture': 'amd64'
                    },
                    'digests': {
                        V2: 'stubDigest'}}}}]
        (koji_session.should_receive('listArchives')
         .and_return(archives))

        name, version, release = koji_build['nvr'].rsplit('-', 2)
        labels = {'com.redhat.component': name, 'version': version, 'release': release}

        image_inspect = {INSPECT_CONFIG: {'Labels': labels}}
        (flexmock(workflow.imageutil)
         .should_receive('get_inspect_for_image')
         .and_return(image_inspect))

        if manifest_list:
            response = flexmock(content=json.dumps(manifest_list))
        else:
            response = {}
        (flexmock(atomic_reactor.util.RegistryClient)
         .should_receive('get_manifest')
         .and_return((response, None)))

        expected_result = {BASE_IMAGE_KOJI_BUILD: KOJI_BUILD,
                           PARENT_IMAGES_KOJI_BUILDS: {
                               ImageName.parse(image_str): KOJI_BUILD}}

        workflow.data.parent_images_digests = {image_str: {V2_LIST: parent_tag}}
        workflow.data.dockerfile_images = DockerfileImages([image_str])
        image_for_key = ImageName.parse(image_str)
        image_for_key.tag = parent_tag
        workflow.data.dockerfile_images[image_str] = image_for_key.to_str()
        self.run_plugin_with_args(workflow, expect_result=expected_result, deep_inspection=True,
                                  pull_registries=[{'url': registry}])

        for log in expected_logs:
            assert log in caplog.text

        for log in not_expected_logs:
            assert log not in caplog.text
