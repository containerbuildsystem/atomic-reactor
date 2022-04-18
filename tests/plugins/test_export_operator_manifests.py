"""
Copyright (c) 2019-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import functools
from pathlib import Path
from tempfile import _RandomNameSequence
from textwrap import dedent

import pytest
import zipfile

from flexmock import flexmock

from atomic_reactor.constants import PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY
from atomic_reactor.plugins.check_and_set_platforms import CheckAndSetPlatformsPlugin
from atomic_reactor.plugins.export_operator_manifests import ExportOperatorManifestsPlugin
from atomic_reactor.plugin import PluginFailedException, PluginsRunner

from atomic_reactor.utils import retries
from tests.constants import TEST_IMAGE
from tests.mock_env import MockEnv
from tests.util import FAKE_CSV

pytestmark = pytest.mark.usefixtures('user_params')

CONTAINER_ID = 'mocked'

PLATFORMS = ["aarch64", "x86_64", "s390x", "ppc64le"]


def mock_source_contents(
        repo_dir: Path,
        has_appregistry_label=False, appregistry_label=False,
        has_bundle_label=True, bundle_label=True
) -> None:
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
        manifests_dir = repo_dir / 'manifests'
        manifests_dir.mkdir()
        with open(manifests_dir / 'operator.clusterserviceversion.yaml', 'w') as f:
            f.write(FAKE_CSV)
    data = '\n'.join([base, operator_appregistry_label, operator_bundle_label, cmd])
    with open(repo_dir / 'Dockerfile', 'w') as f:
        f.write(data)


def extract_manifests_dir(cmd, empty=False, has_archive=True, change_csv_content=False,
                          multiple_csv=False) -> None:
    """Simulate extracting manifests dir from the built image

    The manifest dir is created under the directory specified in cmd:

    build_dir/<platform>/
      manifests/
        stub.yml
        operator.clusterserviceversion.yaml
        another_dir/
          yayml.yml

    :param list cmd: the command to extract manifests
    :param bool empty: whether the archive is empty.
    :param bool has_archive: whether the archive is created
    :param bool change_csv_content: whether to change the default fake CSV
        content for the specific test.
    :param bool multiple_csv: whether to add extra CSV file.
    """
    manifests_dir = Path(cmd[-1].split(':')[-1]) / 'manifests'
    manifests_dir.mkdir()
    another_dir = manifests_dir / 'another_dir'
    another_dir.mkdir()

    if not has_archive:
        return

    if not empty:
        with open(manifests_dir / 'stub.yml', 'w') as f:
            f.write('')
        with open(another_dir / 'yayml.yml', 'w') as f:
            f.write('')

        csv = FAKE_CSV
        if change_csv_content:
            csv += dedent('''\
                  customresourcedefinitions:
            ''')
        with open(manifests_dir / 'operator.clusterserviceversion.yaml', 'w') as f:
            f.write(csv)

        if multiple_csv:
            with open(manifests_dir / 'extra.csv.yaml', 'w') as f:
                f.write(dedent('''\
                    apiVersion: operators.coreos.com/v1alpha1
                    kind: ClusterServiceVersion
                    metadata: {}
                    spec:
                        install: {}
                '''))


def mock_env(workflow, has_appregistry_label=False, appregistry_label=False,
             has_bundle_label=True, bundle_label=True, has_archive=True, scratch=False,
             empty_archive=False, change_csv_content=False,
             multiple_csv=False) -> PluginsRunner:
    mock_source_contents(
        Path(workflow.source.path),
        has_appregistry_label=has_appregistry_label, appregistry_label=appregistry_label,
        has_bundle_label=has_bundle_label, bundle_label=bundle_label
    )

    env = (MockEnv(workflow)
           .for_plugin(ExportOperatorManifestsPlugin.key)
           .set_scratch(scratch)
           .set_plugin_result(CheckAndSetPlatformsPlugin.key, PLATFORMS))

    env.workflow.build_dir.init_build_dirs(PLATFORMS, env.workflow.source)
    env.workflow.data.tag_conf.add_unique_image(TEST_IMAGE)

    mock_oc_image_extract = functools.partial(extract_manifests_dir, empty=empty_archive,
                                              multiple_csv=multiple_csv, has_archive=has_archive,
                                              change_csv_content=change_csv_content)

    (flexmock(retries)
     .should_receive("run_cmd")
     .replace_with(mock_oc_image_extract))

    (flexmock(_RandomNameSequence)
     .should_receive("__next__")
     .and_return('abcdef12'))

    return env.create_runner()


class TestExportOperatorManifests(object):
    @pytest.mark.parametrize('has_appregistry_label', [True, False])
    @pytest.mark.parametrize('appregistry_label', [True, False])
    @pytest.mark.parametrize('has_bundle_label', [True, False])
    @pytest.mark.parametrize('bundle_label', [True, False])
    def test_skip(self, workflow, caplog, has_appregistry_label, appregistry_label,
                  has_bundle_label, bundle_label):

        runner = mock_env(
            workflow, has_appregistry_label=has_appregistry_label,
            has_bundle_label=has_bundle_label, bundle_label=bundle_label,
            appregistry_label=appregistry_label
        )
        if any([
            not (
                (has_appregistry_label and appregistry_label) or
                (has_bundle_label and bundle_label)
            )
        ]):
            result = runner.run()
            assert 'Operator manifests label not set in Dockerfile. Skipping' in caplog.text
            assert result[PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY] is None
        else:
            runner.run()

    @pytest.mark.parametrize('scratch', [True, False])
    def test_export_archive(self, workflow, scratch):
        runner = mock_env(workflow, scratch=scratch)
        result = runner.run()
        archive = result[PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY]

        assert archive
        assert archive.split('/')[-1] == 'operator_manifests.zip'
        assert zipfile.is_zipfile(archive)
        expected = ['stub.yml', 'operator.clusterserviceversion.yaml', 'another_dir/yayml.yml']
        with zipfile.ZipFile(archive, 'r') as z:
            assert len(z.namelist()) == len(expected)
            assert sorted(z.namelist()) == sorted(expected)

    def test_csv_is_changed_in_built_image(self, workflow):
        runner = mock_env(workflow, change_csv_content=True)
        with pytest.raises(PluginFailedException, match='have different content'):
            runner.run()

    def test_multiple_csv_files_inside_built_image(self, workflow):
        runner = mock_env(workflow, multiple_csv=True)
        with pytest.raises(PluginFailedException, match='but contains more'):
            runner.run()

    @pytest.mark.parametrize('has_archive', [True, False, None])
    def test_no_archive(self, workflow, caplog, has_archive):
        runner = mock_env(workflow, has_archive=has_archive)
        if has_archive:
            runner.run()
        else:
            with pytest.raises(PluginFailedException) as exc:
                runner.run()
                if not has_archive:
                    assert 'Could not extract operator manifest files' in caplog.text
                    assert 'Could not extract operator manifest files' in str(exc.value)

    @pytest.mark.parametrize('empty_archive', [True, False])
    @pytest.mark.parametrize('has_bundle_label', [True, False])
    def test_empty_manifests_dir(self, workflow, caplog, empty_archive, has_bundle_label):
        runner = mock_env(workflow, empty_archive=empty_archive,
                          has_bundle_label=has_bundle_label, has_appregistry_label=True)
        if empty_archive and has_bundle_label:
            with pytest.raises(PluginFailedException) as exc:
                runner.run()
            assert 'Missing ClusterServiceVersion in operator manifests' in str(exc.value)
        else:
            runner.run()
            if has_bundle_label:
                assert 'Archiving operator manifests' in caplog.text
            else:
                assert 'Operator manifests label not set in Dockerfile. Skipping' in caplog.text
