"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""


from __future__ import absolute_import

import os
import pytest
import tarfile
import zipfile
from atomic_reactor.constants import PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY
from atomic_reactor.plugins.post_export_operator_manifests import ExportOperatorManifestsPlugin
from atomic_reactor.plugin import PluginFailedException
from docker.errors import NotFound
from flexmock import flexmock
from functools import partial
from platform import machine
from tests.mock_env import MockEnv
from requests import Response

pytestmark = pytest.mark.usefixtures('user_params')

CONTAINER_ID = 'mocked'


def mock_dockerfile(
        tmpdir,
        has_appregistry_label=False, appregistry_label=False,
        has_bundle_label=True, bundle_label=True
):
    base = 'From fedora:30'
    cmd = 'CMD /bin/cowsay moo'
    operator_appregistry_label = ''
    operator_bundle_label = ''
    if has_appregistry_label:
        operator_appregistry_label = 'LABEL com.redhat.delivery.appregistry={}'.format(
            str(appregistry_label).lower())
    if has_bundle_label:
        operator_bundle_label = 'LABEL com.redhat.delivery.operator.bundle={}'.format(
            str(bundle_label).lower())
    data = '\n'.join([base, operator_appregistry_label, operator_bundle_label, cmd])
    tmpdir.join('Dockerfile').write(data)


def generate_archive(tmpdir, empty=False):
    archive_path = os.path.join(str(tmpdir), 'temp.tar')
    archive_tar = tarfile.open(archive_path, 'w')
    manifests_dir = os.path.join(str(tmpdir), 'manifests')
    os.mkdir(manifests_dir)
    another_dir = os.path.join(manifests_dir, 'another_dir')
    os.mkdir(another_dir)
    if not empty:
        open(os.path.join(manifests_dir, 'stub.yml'), 'w').close()
        open(os.path.join(another_dir, 'yayml.yml'), 'w').close()
    archive_tar.add(manifests_dir, arcname='manifests')
    archive_tar.close()
    f = open(archive_path, 'rb')
    for block in iter(partial(f.read, 8), b''):
        yield block
    f.close()
    os.unlink(archive_path)


def mock_env(tmpdir, docker_tasker,
             has_appregistry_label=False, appregistry_label=False,
             has_bundle_label=True, bundle_label=True,
             has_archive=True,
             scratch=False, orchestrator=False, selected_platform=True, empty_archive=False,
             remove_fails=False):
    mock_dockerfile(
        tmpdir,
        has_appregistry_label=has_appregistry_label, appregistry_label=appregistry_label,
        has_bundle_label=has_bundle_label, bundle_label=bundle_label
    )

    env = (MockEnv()
           .for_plugin('postbuild', ExportOperatorManifestsPlugin.key)
           .set_scratch(scratch))
    if orchestrator:
        env.make_orchestrator()

    env.workflow.builder.set_df_path(str(tmpdir))

    mock_stream = generate_archive(tmpdir, empty_archive)
    if selected_platform:
        env.set_plugin_args({'operator_manifests_extract_platform': machine(),
                             'platform': machine()})

    (flexmock(docker_tasker.tasker.d.wrapped)
     .should_receive('create_container')
     .with_args(env.workflow.image, command=["/bin/bash"])
     .and_return({'Id': CONTAINER_ID}))

    if remove_fails:
        (flexmock(docker_tasker.tasker.d.wrapped)
         .should_receive('remove_container')
         .with_args(CONTAINER_ID)
         .and_raise(Exception('error')))
    else:
        (flexmock(docker_tasker.tasker.d.wrapped)
         .should_receive('remove_container')
         .with_args(CONTAINER_ID))

    if has_archive:
        (flexmock(docker_tasker.tasker.d.wrapped)
         .should_receive('get_archive')
         .with_args(CONTAINER_ID, '/manifests')
         .and_return(mock_stream, {}))
    elif has_archive is not None:
        response = Response()
        response.status_code = 404
        (flexmock(docker_tasker.tasker.d.wrapped)
         .should_receive('get_archive')
         .with_args(CONTAINER_ID, '/manifests')
         .and_raise(NotFound('Not found', response=response)))
    else:
        response = Response()
        response.status_code = 500
        (flexmock(docker_tasker.tasker.d.wrapped)
         .should_receive('get_archive')
         .with_args(CONTAINER_ID, '/manifests')
         .and_raise(Exception('error')))

    return env.create_runner(docker_tasker)


class TestExportOperatorManifests(object):
    @pytest.mark.parametrize('scratch', [True, False])
    @pytest.mark.parametrize('has_appregistry_label', [True, False])
    @pytest.mark.parametrize('appregistry_label', [True, False])
    @pytest.mark.parametrize('has_bundle_label', [True, False])
    @pytest.mark.parametrize('bundle_label', [True, False])
    @pytest.mark.parametrize('orchestrator', [True, False])
    @pytest.mark.parametrize('selected_platform', [True, False])
    def test_skip(self, docker_tasker, tmpdir, caplog, scratch,
                  has_appregistry_label, appregistry_label,
                  has_bundle_label, bundle_label,
                  orchestrator, selected_platform):

        runner = mock_env(
            tmpdir, docker_tasker,
            has_appregistry_label=has_appregistry_label,
            has_bundle_label=has_bundle_label, bundle_label=bundle_label,
            appregistry_label=appregistry_label,
            scratch=scratch,
            orchestrator=orchestrator, selected_platform=selected_platform
        )
        result = runner.run()
        if any([
            not (
                (has_appregistry_label and appregistry_label) or
                (has_bundle_label and bundle_label)
            ),
            scratch,
            orchestrator,
            not selected_platform
        ]):
            assert 'Skipping' in caplog.text
            assert result[PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY] is None
        else:
            assert 'Skipping' not in caplog.text

    def test_export_archive(self, docker_tasker, tmpdir):
        runner = mock_env(tmpdir, docker_tasker)
        result = runner.run()
        archive = result[PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY]
        assert archive
        assert archive.split('/')[-1] == 'operator_manifests.zip'
        assert zipfile.is_zipfile(archive)
        expected = ['stub.yml', 'another_dir/yayml.yml']
        with zipfile.ZipFile(archive, 'r') as z:
            assert len(z.namelist()) == len(expected)
            assert sorted(z.namelist()) == sorted(expected)

    @pytest.mark.parametrize('remove_fails', [True, False])
    @pytest.mark.parametrize('has_archive', [True, False, None])
    def test_no_archive(self, docker_tasker, tmpdir, caplog, remove_fails, has_archive):
        runner = mock_env(tmpdir, docker_tasker, has_archive=has_archive,
                          remove_fails=remove_fails)
        if has_archive:
            runner.run()
            if remove_fails:
                assert 'Failed to remove container' in caplog.text
        else:
            with pytest.raises(PluginFailedException) as exc:
                runner.run()
                if not has_archive:
                    assert 'Could not extract operator manifest files' in caplog.text
                    assert 'Could not extract operator manifest files' in str(exc.value)
                if remove_fails:
                    assert 'Failed to remove container' in caplog.text

    @pytest.mark.parametrize('empty_archive', [True, False])
    def test_emty_manifests_dir(self, docker_tasker, tmpdir, caplog, empty_archive):
        runner = mock_env(tmpdir, docker_tasker, empty_archive=empty_archive)
        if empty_archive:
            with pytest.raises(PluginFailedException) as exc:
                runner.run()
                assert 'Empty operator manifests directory' in caplog.text
                assert 'Empty operator manifests directory' in str(exc.value)
        else:
            runner.run()
