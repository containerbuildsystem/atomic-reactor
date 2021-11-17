"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from functools import partial
from pathlib import Path
from textwrap import dedent

import pytest

from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.pre_check_user_settings import CheckUserSettingsPlugin
from atomic_reactor.util import df_parser, DockerfileImages
from atomic_reactor.constants import (
    DOCKERFILE_FILENAME,
    REPO_CONTENT_SETS_CONFIG,
    REPO_FETCH_ARTIFACTS_KOJI,
    REPO_FETCH_ARTIFACTS_URL,
)

from tests.mock_env import MockEnv
from tests.stubs import StubSource

pytestmark = pytest.mark.usefixtures('user_params')


def mock_dockerfile(source_dir: Path, labels, from_scratch=True):
    base = 'FROM scratch' if from_scratch else 'FROM fedora:30'
    cmd = 'CMD /bin/cowsay moo'
    extra_labels = [
        'LABEL {}'.format(label)
        for label in labels

    ]
    data = '\n'.join([base] + extra_labels + [cmd])
    source_dir.joinpath(DOCKERFILE_FILENAME).write_text(data, "utf-8")


def mock_dockerfile_multistage(source_dir: Path, labels, from_scratch=False):
    data = """\
    FROM scratch
    RUN echo *

    {data_from}
    {extra_labels}
    CMD /bin/cowsay moo
    """
    extra_labels = '\n'.join(
        'LABEL {}'.format(label)
        for label in labels
    )
    data_from = 'FROM scratch' if from_scratch else 'FROM fedora:30'
    source_dir.joinpath(DOCKERFILE_FILENAME).write_text(
        data.format(data_from=data_from, extra_labels=extra_labels), "utf-8"
    )


class FakeSource(StubSource):
    """Fake source for config files validation"""

    def __init__(self, source_dir: Path):
        """Initialize this fake source

        :param dockerfile_path: the path to the dockerfile, not including the dockerfile filename.
        :type dockerfile_path: py.path.LocalPath
        """
        super().__init__()
        self.path = str(source_dir)
        self.dockerfile_path = str(source_dir / DOCKERFILE_FILENAME)

    def get_build_file_path(self):
        """Ensure the validations run against distgit config files"""
        return self.dockerfile_path, self.path


def mock_env(workflow, source_dir: Path, labels=None, flatpak=False, dockerfile_f=mock_dockerfile,
             isolated=None):
    """Mock test environment

    :param workflow: a DockerBuildWorkflow object for a specific test.
    :type workflow: DockerBuildWorkflow
    :param source_dir: path to the source directory holding the dockerfile to be created.
    :type source_dir: pathlib.Path
    :param labels: an iterable labels set for testing operator bundle or appregistry build.
    :type labels: iterable[str]
    :param bool flatpak: a flag to indicate whether the test is for a flatpak build.
    :param callable dockerfile_f: a function to create fake dockerfile. Different test could pass a
        specific function for itself.
    :param bool isolated: a flag to indicated if build is isolated
    """
    # Make sure the version label will be presented in labels
    if not labels:
        labels = ['version="1.0"']
    elif not any([label.startswith('version') for label in labels]):
        labels.append('version="1.0"')

    if not flatpak:
        # flatpak build has no Dockefile
        dockerfile_f(source_dir, labels)

    env = MockEnv(workflow).for_plugin(
        'prebuild', CheckUserSettingsPlugin.key, {'flatpak': flatpak}
    )
    env.workflow.source = FakeSource(source_dir)

    if isolated is not None:
        env.set_isolated(isolated)

    dfp = df_parser(str(source_dir))
    env.workflow._df_path = str(source_dir)
    env.workflow.dockerfile_images = DockerfileImages([] if flatpak else dfp.parent_images)

    return env.create_runner()


class TestDockerfileChecks(object):
    """
    Test checks related to Dockerfile
    """

    @pytest.mark.parametrize('labels, expected_fail', (
        (['version="0.1.test.label.version_with_underscore"'], False),
        (['version="0.1/.test.label.version|with|error"'], True),
    ))
    def test_label_version_check(self, workflow, source_dir, labels, expected_fail):
        """Dockerfile label version can't contain '/' character"""
        runner = mock_env(workflow, source_dir, labels=labels)

        if expected_fail:
            with pytest.raises(PluginFailedException) as e:
                runner.run()
            assert "Dockerfile version label can't contain '/' character" in str(e.value)
        else:
            runner.run()

    @pytest.mark.parametrize('labels,expected_fail', (
        (['com.redhat.delivery.appregistry=true',
          'com.redhat.delivery.operator.bundle=true'],
         True),
        (['com.redhat.delivery.appregistry=true',
          'com.redhat.delivery.operator.bundle=false'],
         False),
        (['com.redhat.delivery.appregistry=true'], False),
        (['com.redhat.delivery.operator.bundle=true'], False),
    ))
    def test_mutual_exclusivity_of_labels(self, workflow, source_dir, labels, expected_fail):
        """Appregistry and operator.bundle labels are mutually exclusive"""
        runner = mock_env(workflow, source_dir, labels=labels)

        if expected_fail:
            with pytest.raises(PluginFailedException) as e:
                runner.run()
            assert 'only one of labels' in str(e.value)
        else:
            runner.run()

    @pytest.mark.parametrize('from_scratch,multistage,labels,expected_fail', (
        [True, False, ['com.redhat.delivery.operator.bundle=true'], False],
        [False, False, ['com.redhat.delivery.operator.bundle=true'], True],
        [True, True, ['com.redhat.delivery.operator.bundle=true'], True],
        [False, True, ['com.redhat.delivery.operator.bundle=true'], True],
        [True, False, [], False],
        [False, False, [], False],
        [True, True, [], False],
    ))
    def test_operator_bundle_from_scratch(
        self, workflow, source_dir, from_scratch, multistage, labels, expected_fail
    ):
        """Operator bundle can be only single stage and FROM scratch"""
        if multistage:
            dockerfile_f = mock_dockerfile_multistage
        else:
            dockerfile_f = mock_dockerfile

        dockerfile_f = partial(dockerfile_f, from_scratch=from_scratch)

        runner = mock_env(workflow, source_dir, dockerfile_f=dockerfile_f, labels=labels)

        if expected_fail:
            with pytest.raises(PluginFailedException) as e:
                runner.run()
            assert 'Operator bundle build can be only' in str(e.value)
        else:
            runner.run()

    def test_flatpak_skip_dockerfile_check(self, workflow, source_dir, caplog):
        """Flatpak builds have no user Dockerfiles, dockefile check must be skipped"""
        runner = mock_env(workflow, source_dir, flatpak=True)
        runner.run()

        assert 'Skipping Dockerfile checks' in caplog.text


def write_fetch_artifacts_url(source_dir: Path, make_mistake=False):
    if make_mistake:
        content = dedent('''
            - sha4096: 305aa706018b1089e5b82528b601541f
              target: foo.jar
              url: url
        ''')
    else:
        content = dedent('''
            - md5: 305aa706018b1089e5b82528b601541f
              target: foo.jar
              url: http://somewhere/foo.jar
        ''')
    source_dir.joinpath(REPO_FETCH_ARTIFACTS_URL).write_text(content, "utf-8")


def write_fetch_artifacts_koji(source_dir: Path, make_mistake=False):
    if make_mistake:
        content = dedent('''
            - archives:
              - filename: jmx_prometheus_javaagent-0.3.1.redhat-00006.jar
                group_id: io.prometheus.jmx
        ''')
    else:
        content = dedent('''
            - nvr: io.prometheus.jmx-parent-0.3.1.redhat_00006-1
              archives:
              - filename: jmx_prometheus_javaagent-0.3.1.redhat-00006.jar
                group_id: io.prometheus.jmx
        ''')
    source_dir.joinpath(REPO_FETCH_ARTIFACTS_KOJI).write_text(content, "utf-8")


def write_content_sets_yml(source_dir: Path, make_mistake=False):
    if make_mistake:
        content = dedent('''
            x86_64:
            - rhel-7-server-optional-rpms
            - rhel-7-server-DOT
        ''')
    else:
        content = dedent('''
            ---
            x86_64:
            - rhel-7-server-optional-rpms
            - rhel-7-server-rpms
        ''')
    source_dir.joinpath(REPO_CONTENT_SETS_CONFIG).write_text(content, "utf-8")


class TestValidateUserConfigFiles(object):
    """Test the validate_user_config_files"""

    def test_validate_the_config_files(self, workflow, source_dir):
        write_fetch_artifacts_koji(source_dir)
        write_fetch_artifacts_url(source_dir)
        write_content_sets_yml(source_dir)

        runner = mock_env(workflow, source_dir)
        runner.run()

    def test_catch_invalid_fetch_artifacts_url(self, workflow, source_dir):
        write_fetch_artifacts_url(source_dir, make_mistake=True)

        runner = mock_env(workflow, source_dir)
        with pytest.raises(PluginFailedException, match="'sha4096' was unexpected"):
            runner.run()

    def test_catch_invalid_fetch_artifacts_koji(self, workflow, source_dir):
        write_fetch_artifacts_koji(source_dir, make_mistake=True)

        runner = mock_env(workflow, source_dir)
        with pytest.raises(PluginFailedException, match="'nvr' is a required property"):
            runner.run()

    def test_catch_invalid_content_sets(self, workflow, source_dir):
        write_content_sets_yml(source_dir, make_mistake=True)

        runner = mock_env(workflow, source_dir)
        with pytest.raises(PluginFailedException, match="validating 'pattern' has failed"):
            runner.run()


class TestIsolatedBuildChecks(object):
    """Test isolated_build_checks"""

    @pytest.mark.parametrize(
        'isolated,bundle,from_scratch,expected_fail',
        [
            (True, True, True, False),
            # (True, True, False, True),  # invalid, bundle must be FROM scratch
            (True, False, True, True),
            (True, False, False, False),
            (False, True, True, False),
            # (False, True, False, False), # invalid, bundle must be FROM scratch
            (False, False, True, False),
            (False, False, False, False),
        ]
    )
    def test_isolated_from_scratch_build(
        self, workflow, source_dir, isolated, bundle, from_scratch, expected_fail
    ):
        """Test if isolated FROM scratch builds are prohibited except
        operator bundle builds"""
        labels = ['com.redhat.delivery.operator.bundle=true'] if bundle else []

        dockerfile_f = partial(mock_dockerfile, from_scratch=from_scratch)

        runner = mock_env(workflow, source_dir,
                          dockerfile_f=dockerfile_f, labels=labels, isolated=isolated)
        if expected_fail:
            with pytest.raises(PluginFailedException) as exc_info:
                runner.run()

            assert '"FROM scratch" image build cannot be isolated ' in str(exc_info.value)
        else:
            runner.run()
