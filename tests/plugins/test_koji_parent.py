"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import json
import os

try:
    import koji as koji
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
    import koji as koji

import atomic_reactor
from atomic_reactor.constants import (
    INSPECT_CONFIG, BASE_IMAGE_KOJI_BUILD, PARENT_IMAGES_KOJI_BUILDS
)
from atomic_reactor.build import InsideBuilder
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_koji_parent import KojiParentPlugin
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor.util import ImageName, get_manifest_media_type
from atomic_reactor.constants import SCRATCH_FROM
from flexmock import flexmock
from tests.constants import MOCK, MOCK_SOURCE

import pytest

if MOCK:
    from tests.docker_mock import mock_docker


KOJI_HUB = 'http://koji.com/hub'

KOJI_BUILD_ID = 123456789

KOJI_BUILD_NVR = 'base-image-1.0-99'

KOJI_STATE_COMPLETE = koji.BUILD_STATES['COMPLETE']

V2_LIST = get_manifest_media_type('v2_list')
V2 = get_manifest_media_type('v2')
KOJI_EXTRA = {'image': {'index': {'digests': {V2_LIST: 'stubDigest'}}}}

KOJI_STATE_DELETED = koji.BUILD_STATES['DELETED']

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


class MockInsideBuilder(InsideBuilder):
    def __init__(self):
        self.tasker = flexmock()
        self.base_image = ImageName(repo='Fedora', tag='22')
        self.original_base_image = ImageName(repo='Fedora', tag='22')
        self.base_from_scratch = False
        self.custom_base_image = False
        self.parent_images = {ImageName.parse('base'): ImageName.parse('base:stubDigest')}
        base_inspect = {INSPECT_CONFIG: {'Labels': BASE_IMAGE_LABELS.copy()}}
        self._parent_images_inspect = {ImageName.parse('base:stubDigest'): base_inspect}
        self.parent_images_digests = {'base:latest': {V2_LIST: 'stubDigest'}}
        self.image_id = 'image_id'
        self.image = 'image'
        self._df_path = 'df_path'
        self.df_dir = 'df_dir'

    @property
    def source(self):
        result = flexmock()
        setattr(result, 'dockerfile_path', '/')
        setattr(result, 'path', '/tmp')
        return result


@pytest.fixture()
def workflow():
    if MOCK:
        mock_docker()
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.builder = MockInsideBuilder()
    base_inspect = {INSPECT_CONFIG: {'Labels': BASE_IMAGE_LABELS.copy()}}
    flexmock(workflow.builder, base_image_inspect=base_inspect)

    return workflow


@pytest.fixture()
def koji_session():
    session = flexmock()
    flexmock(session).should_receive('getBuild').with_args(KOJI_BUILD_NVR).and_return(KOJI_BUILD)
    flexmock(session).should_receive('krb_login').and_return(True)
    flexmock(koji).should_receive('ClientSession').and_return(session)
    return session


class TestKojiParent(object):

    def test_koji_build_found(self, workflow, koji_session, reactor_config_map):  # noqa
        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)

    @pytest.mark.skip(reason="Raising for manifests_mismatches is disabled")
    def test_koji_build_no_extra(self, workflow, koji_session, reactor_config_map):  # noqa
        koji_no_extra = {'nvr': KOJI_BUILD_NVR, 'id': KOJI_BUILD_ID, 'state': KOJI_STATE_COMPLETE}
        (flexmock(koji_session)
            .should_receive('getBuild')
            .with_args(KOJI_BUILD_NVR)
            .and_return(koji_no_extra))
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)
        assert 'does not have manifest digest data' in str(exc_info)

    def test_koji_build_retry(self, workflow, koji_session, reactor_config_map):  # noqa
        (flexmock(koji_session)
            .should_receive('getBuild')
            .with_args(KOJI_BUILD_NVR)
            .and_return(None)
            .and_return(None)
            .and_return(None)
            .and_return(None)
            .and_return(KOJI_BUILD)
            .times(5))

        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)

    def test_koji_ssl_certs_used(self, tmpdir, workflow, koji_session, reactor_config_map):  # noqa
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
        self.run_plugin_with_args(workflow, plugin_args, reactor_config_map=reactor_config_map)

    def test_koji_build_not_found(self, workflow, koji_session, reactor_config_map):  # noqa
        (flexmock(koji_session)
            .should_receive('getBuild')
            .with_args(KOJI_BUILD_NVR)
            .and_return(None))

        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow, {'poll_timeout': 0.01},
                                      reactor_config_map=reactor_config_map)
        assert 'KojiParentBuildMissing' in str(exc_info.value)

    def test_koji_build_deleted(self, workflow, koji_session, reactor_config_map):  # noqa
        (flexmock(koji_session)
            .should_receive('getBuild')
            .with_args(KOJI_BUILD_NVR)
            .and_return(DELETED_KOJI_BUILD))

        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)
        assert 'KojiParentBuildMissing' in str(exc_info.value)
        assert 'state is not COMPLETE' in str(exc_info.value)

    def test_base_image_not_inspected(self, workflow, koji_session, reactor_config_map):  # noqa
        del workflow.builder.base_image_inspect[INSPECT_CONFIG]
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)
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
    def test_base_image_missing_labels(self, workflow, koji_session, remove_labels, exp_result,
                                       reactor_config_map, external, caplog):
        base_tag = ImageName.parse('base:stubDigest')
        workflow.builder.base_image_inspect[INSPECT_CONFIG]['Labels'] =\
            BASE_IMAGE_LABELS_W_ALIASES.copy()
        workflow.builder._parent_images_inspect[base_tag][INSPECT_CONFIG]['Labels'] =\
            BASE_IMAGE_LABELS_W_ALIASES.copy()
        for label in remove_labels:
            del workflow.builder.base_image_inspect[INSPECT_CONFIG]['Labels'][label]
            del workflow.builder._parent_images_inspect[base_tag][INSPECT_CONFIG]['Labels'][label]
        if not exp_result:
            if not (external and reactor_config_map):
                with pytest.raises(PluginFailedException) as exc:
                    self.run_plugin_with_args(workflow, expect_result=exp_result,
                                              reactor_config_map=reactor_config_map,
                                              external_base=external)
                assert 'Was this image built in OSBS?' in str(exc)
            else:
                result = {PARENT_IMAGES_KOJI_BUILDS: {ImageName.parse('base'): None}}
                self.run_plugin_with_args(workflow, expect_result=result,
                                          reactor_config_map=reactor_config_map,
                                          external_base=external)
                assert 'Was this image built in OSBS?' in caplog.text
        else:
            self.run_plugin_with_args(workflow, expect_result=exp_result,
                                      reactor_config_map=reactor_config_map)

    @pytest.mark.parametrize('media_version', ['v2_list', 'v2'])
    @pytest.mark.parametrize('koji_mtype', [True, False])
    @pytest.mark.parametrize('parent_tags', [
                             ['miss', 'stubDigest', 'stubDigest'],
                             ['stubDigest', 'miss', 'stubDigest'],
                             ['stubDigest', 'stubDigest', 'miss'],
                             ['miss', 'miss', 'miss'],
                             ['stubDigest', 'stubDigest', 'stubDigest']])
    @pytest.mark.parametrize('special_base', [False, 'scratch', 'custom'])  # noqa: F811
    def test_multiple_parent_images(self, workflow, koji_session, reactor_config_map, koji_mtype,
                                    special_base, parent_tags, media_version, caplog):
        parent_images = {
            ImageName.parse('somebuilder'): ImageName.parse('somebuilder:{}'
                                                            .format(parent_tags[0])),
            ImageName.parse('otherbuilder'): ImageName.parse('otherbuilder:{}'
                                                             .format(parent_tags[1])),
            ImageName.parse('base'): ImageName.parse('base:{}'.format(parent_tags[2])),
        }
        media_type = get_manifest_media_type(media_version)
        workflow.builder.parent_images_digests = {}
        for parent in parent_images:
            dgst = parent_images[parent].tag
            workflow.builder.parent_images_digests[parent.to_str()] = {media_type: dgst}
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
            (workflow.builder.tasker
                .should_receive('inspect_image')
                .with_args(parent_images[ImageName.parse(img)])
                .and_return(image_inspects[img]))
            (koji_session.should_receive('getBuild')
                .with_args(koji_builds[img]['nvr'])
                .and_return(koji_builds[img]))
            koji_expects[ImageName.parse(img)] = build

        if special_base == 'scratch':
            workflow.builder.set_base_image(SCRATCH_FROM)
        elif special_base == 'custom':
            workflow.builder.set_base_image('koji/image-build')
            parent_images[ImageName.parse('koji/image-build')] = None
        else:
            workflow.builder.set_base_image('basetag')
            workflow.builder.base_image_inspect.update(image_inspects['base'])
        workflow.builder.parent_images = parent_images

        expected = {
            BASE_IMAGE_KOJI_BUILD: koji_builds['base'],
            PARENT_IMAGES_KOJI_BUILDS: koji_expects,
        }
        if special_base:
            del expected[BASE_IMAGE_KOJI_BUILD]

        if not koji_mtype:
            self.run_plugin_with_args(
                workflow, expect_result=expected, reactor_config_map=reactor_config_map
            )
            assert 'does not have manifest digest data for the expected media type' in caplog.text
        elif 'miss' in parent_tags or not koji_mtype:
            # TODO: here we should capture an exception instead
            self.run_plugin_with_args(
                workflow, expect_result=expected, reactor_config_map=reactor_config_map
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
                workflow, expect_result=expected, reactor_config_map=reactor_config_map
            )

    def test_unexpected_digest_data(self, workflow, koji_session, reactor_config_map):  # noqa
        workflow.builder.parent_images_digests = {'base:latest': {'unexpected_type': 'stubDigest'}}
        with pytest.raises(PluginFailedException) as exc_info:
            self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map)
        assert 'Unexpected parent image digest data' in str(exc_info)

    @pytest.mark.parametrize('feature_flag', [True, False])
    @pytest.mark.parametrize('parent_tag', ['stubDigest', 'wrongDigest'])
    @pytest.mark.parametrize('has_registry', [True, False])
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
    def test_deep_digest_inspection(self, workflow, koji_session, reactor_config_map, parent_tag,
                                    caplog, has_registry, manifest_list, feature_flag):  # noqa
        image_str = 'base'
        if has_registry:
            image_str = '/'.join(['example.com', image_str])
        extra = {'image': {'index': {'digests': {V2_LIST: 'stubDigest'}}}}
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

        (workflow.builder
         .should_receive('parent_image_inspect')
         .and_return(image_inspect))
        if manifest_list:
            response = flexmock(content=json.dumps(manifest_list))
        else:
            response = {}
        flexmock(atomic_reactor.util).should_receive('get_manifest').and_return((response, None))

        expected_result = {BASE_IMAGE_KOJI_BUILD: KOJI_BUILD,
                           PARENT_IMAGES_KOJI_BUILDS: {
                               ImageName.parse(image_str): KOJI_BUILD}}

        workflow.builder.parent_images_digests = {image_str+':latest': {V2_LIST: parent_tag}}
        parent_images = {
            ImageName.parse(image_str): ImageName.parse('{}:{}'.format(image_str, parent_tag)),
        }
        workflow.builder.parent_images = parent_images
        self.run_plugin_with_args(workflow, reactor_config_map=reactor_config_map,
                                  expect_result=expected_result, deep_inspection=feature_flag)

        rebuild_str = 'This parent image MUST be rebuilt'
        if parent_tag == 'stubDigest':
            assert rebuild_str not in caplog.text
        else:
            if not feature_flag and reactor_config_map:
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
                    else:
                        assert 'do not match koji archive refs' in caplog.text
                        assert rebuild_str in caplog.text
                else:
                    fetch_error = 'Could not fetch manifest list for {}:latest'.format(image_str)
                    assert fetch_error in caplog.text
                    assert rebuild_str in caplog.text

    def run_plugin_with_args(self, workflow, plugin_args=None, expect_result=True,  # noqa
                             reactor_config_map=False, external_base=False, deep_inspection=True):
        plugin_args = plugin_args or {}
        plugin_args.setdefault('koji_hub', KOJI_HUB)
        plugin_args.setdefault('poll_interval', 0.01)
        plugin_args.setdefault('poll_timeout', 1)

        if reactor_config_map:

            koji_map = {
                'hub_url': KOJI_HUB,
                'root_url': '',
                'auth': {}
            }
            if 'koji_ssl_certs_dir' in plugin_args:
                koji_map['auth']['ssl_certs_dir'] = plugin_args['koji_ssl_certs_dir']
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1, 'koji': koji_map,
                               'deep_manifest_list_inspection': deep_inspection,
                               'skip_koji_check_for_base_image': external_base})

        runner = PreBuildPluginsRunner(
            workflow.builder.tasker,
            workflow,
            [{'name': KojiParentPlugin.key, 'args': plugin_args}]
        )

        result = runner.run()
        if expect_result is True:
            expected_result = {BASE_IMAGE_KOJI_BUILD: KOJI_BUILD,
                               PARENT_IMAGES_KOJI_BUILDS: {
                                   ImageName.parse('base:latest'): KOJI_BUILD}}
        elif expect_result is False:
            expected_result = None
        else:  # param provided the expected result
            expected_result = expect_result

        assert result[KojiParentPlugin.key] == expected_result
