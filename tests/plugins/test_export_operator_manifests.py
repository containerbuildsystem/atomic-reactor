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
from atomic_reactor import util
from atomic_reactor.constants import (
    PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY,
    PLUGIN_CHECK_AND_SET_PLATFORMS_KEY)
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugins.post_export_operator_manifests import ExportOperatorManifestsPlugin
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from docker.errors import NotFound
from flexmock import flexmock
from functools import partial
from platform import machine
from tests.constants import TEST_IMAGE
from tests.stubs import StubInsideBuilder, StubSource
from requests import Response


CONTAINER_ID = 'mocked'


def mock_dockerfile(tmpdir, has_label=True, label=True):
    base = 'From fedora:30'
    cmd = 'CMD /bin/cowsay moo'
    operator_label = ''
    if has_label:
        operator_label = 'LABEL com.redhat.delivery.appregistry={}'.format(str(label).lower())
    data = '\n'.join([base, operator_label, cmd])
    tmpdir.join('Dockerfile').write(data)


def mock_workflow(tmpdir):
    workflow = DockerBuildWorkflow({"provider": "git", "uri": "asd"}, TEST_IMAGE)
    workflow.source = StubSource()
    builder = StubInsideBuilder().for_workflow(workflow)
    builder.set_df_path(str(tmpdir))
    builder.tasker = flexmock()
    workflow.builder = flexmock(builder)
    return workflow


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


def mock_env(tmpdir, docker_tasker, has_label=True, label=True, has_archive=True,
             scratch=False, orchestrator=False, selected_platform=True, empty_archive=False,
             remove_fails=False):
    build_json = {'metadata': {'labels': {'scratch': scratch}}}
    flexmock(util).should_receive('get_build_json').and_return(build_json)
    mock_dockerfile(tmpdir, has_label, label)
    workflow = mock_workflow(tmpdir)
    workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = orchestrator
    mock_stream = generate_archive(tmpdir, empty_archive)
    plugin_conf = [{'name': ExportOperatorManifestsPlugin.key}]
    if selected_platform:
        plugin_conf[0]['args'] = {'operator_manifests_extract_platform': machine(),
                                  'platform': machine()}
    runner = PostBuildPluginsRunner(docker_tasker, workflow, plugin_conf)

    (flexmock(docker_tasker.d.wrapped)
     .should_receive('create_container')
     .with_args(workflow.image, command=["/bin/bash"])
     .and_return({'Id': CONTAINER_ID}))

    if remove_fails:
        (flexmock(docker_tasker.d.wrapped)
         .should_receive('remove_container')
         .with_args(CONTAINER_ID)
         .and_raise(Exception('error')))
    else:
        (flexmock(docker_tasker.d.wrapped)
         .should_receive('remove_container')
         .with_args(CONTAINER_ID))

    if has_archive:
        (flexmock(docker_tasker.d.wrapped)
         .should_receive('get_archive')
         .with_args(CONTAINER_ID, '/manifests')
         .and_return(mock_stream, {}))
    elif has_archive is not None:
        response = Response()
        response.status_code = 404
        (flexmock(docker_tasker.d.wrapped)
         .should_receive('get_archive')
         .with_args(CONTAINER_ID, '/manifests')
         .and_raise(NotFound('Not found', response=response)))
    else:
        response = Response()
        response.status_code = 500
        (flexmock(docker_tasker.d.wrapped)
         .should_receive('get_archive')
         .with_args(CONTAINER_ID, '/manifests')
         .and_raise(Exception('error')))

    return runner


class TestExportOperatorManifests(object):
    @pytest.mark.parametrize('scratch', [True, False])
    @pytest.mark.parametrize('has_label', [True, False])
    @pytest.mark.parametrize('label', [True, False])
    @pytest.mark.parametrize('orchestrator', [True, False])
    @pytest.mark.parametrize('selected_platform', [True, False])
    def test_skip(self, docker_tasker, tmpdir, caplog, scratch, has_label,
                  label, orchestrator, selected_platform):

        runner = mock_env(tmpdir, docker_tasker, has_label=has_label, label=label, scratch=scratch,
                          orchestrator=orchestrator, selected_platform=selected_platform)
        result = runner.run()
        if any([not has_label, not label, scratch, orchestrator, not selected_platform]):
            assert 'Skipping' in caplog.text
            assert result[PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY] is None

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
                    assert 'Could not extract operator manifest files' in str(exc)
                if remove_fails:
                    assert 'Failed to remove container' in caplog.text

    @pytest.mark.parametrize('empty_archive', [True, False])
    def test_emty_manifests_dir(self, docker_tasker, tmpdir, caplog, empty_archive):
        runner = mock_env(tmpdir, docker_tasker, empty_archive=empty_archive)
        if empty_archive:
            with pytest.raises(PluginFailedException) as exc:
                runner.run()
                assert 'Empty operator manifests directory' in caplog.text
                assert 'Empty operator manifests directory' in str(exc)
        else:
            runner.run()
