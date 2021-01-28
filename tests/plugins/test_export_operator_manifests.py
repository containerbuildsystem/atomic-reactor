"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from textwrap import dedent

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
from tests.util import mock_manifests_dir, FAKE_CSV

pytestmark = pytest.mark.usefixtures('user_params')

CONTAINER_ID = 'mocked'


def mock_dockerfile(
        repo_dir,
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
    repo_dir.join('Dockerfile').write(data)


def generate_archive(tmpdir, empty=False, change_csv_content=False, multiple_csv=False):
    """Generate a fake tar archive

    The tar archive contains everything under the specified tmpdir:

    $tmpdir
      manifests/
        stub.yml
        operator.clusterserviceversion.yaml
        another_dir/
          yayml.yml

    :param tmpdir: the directory inside with to create the fake generated archive.
    :type tmpdir: py.path.LocalPath
    :param bool empty: whether the archive is empty.
    :param bool change_csv_content: whether to change the default fake CSV
        content for the specific test.
    :param bool multiple_csv: whether to add extra CSV file.
    :return: a generator to return archive content. This is for mocking get_archive.
    :rtype: generator
    """
    manifests_dir = tmpdir.join('manifests').mkdir()
    another_dir = manifests_dir.join('another_dir').mkdir()
    if not empty:
        manifests_dir.join('stub.yml').write('')
        another_dir.join('yayml.yml').write('')

        csv = FAKE_CSV
        if change_csv_content:
            csv += dedent('''\
                  customresourcedefinitions:
            ''')
        manifests_dir.join('operator.clusterserviceversion.yaml').write(csv)

        if multiple_csv:
            manifests_dir.join('extra.csv.yaml').write(dedent('''\
                apiVersion: operators.coreos.com/v1alpha1
                kind: ClusterServiceVersion
                metadata: {}
                spec:
                    install: {}
            '''))

    archive_path = tmpdir.join('temp.tar')
    with tarfile.open(str(archive_path), 'w') as archive_tar:
        archive_tar.add(manifests_dir, arcname='manifests')

    f = open(str(archive_path), 'rb')
    for block in iter(partial(f.read, 8), b''):
        yield block
    f.close()
    archive_path.remove()


def mock_env(tmpdir, docker_tasker,
             has_appregistry_label=False, appregistry_label=False,
             has_bundle_label=True, bundle_label=True,
             has_archive=True,
             scratch=False, orchestrator=False, selected_platform=True, empty_archive=False,
             remove_fails=False, change_csv_content=False, multiple_csv=False):
    repo_dir = tmpdir.join('test-operator').mkdir()
    mock_dockerfile(
        repo_dir,
        has_appregistry_label=has_appregistry_label, appregistry_label=appregistry_label,
        has_bundle_label=has_bundle_label, bundle_label=bundle_label
    )
    manifests_dir = mock_manifests_dir(repo_dir)

    env = (MockEnv()
           .for_plugin('postbuild', ExportOperatorManifestsPlugin.key)
           .set_scratch(scratch))
    if orchestrator:
        env.make_orchestrator()

    class MockSource(object):
        @property
        def manifests_dir(self):
            return manifests_dir

    # Set a new source object, only the manifests_dir property is required for tests.
    setattr(env.workflow, 'source', MockSource())
    env.workflow.builder.set_df_path(str(repo_dir))

    mock_stream = generate_archive(tmpdir, empty_archive, change_csv_content, multiple_csv)
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
        expected = ['stub.yml', 'operator.clusterserviceversion.yaml', 'another_dir/yayml.yml']
        with zipfile.ZipFile(archive, 'r') as z:
            assert len(z.namelist()) == len(expected)
            assert sorted(z.namelist()) == sorted(expected)

    def test_csv_is_changed_in_built_image(self, docker_tasker, tmpdir):
        runner = mock_env(tmpdir, docker_tasker, change_csv_content=True)
        with pytest.raises(PluginFailedException, match='have different content'):
            runner.run()

    def test_multiple_csv_files_inside_built_image(self, docker_tasker, tmpdir):
        runner = mock_env(tmpdir, docker_tasker, multiple_csv=True)
        with pytest.raises(PluginFailedException, match='but contains more'):
            runner.run()

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
    def test_empty_manifests_dir(self, docker_tasker, tmpdir, caplog, empty_archive):
        runner = mock_env(tmpdir, docker_tasker, empty_archive=empty_archive)
        if empty_archive:
            with pytest.raises(PluginFailedException) as exc:
                runner.run()
                assert 'Empty operator manifests directory' in caplog.text
                assert 'Empty operator manifests directory' in str(exc.value)
        else:
            runner.run()
