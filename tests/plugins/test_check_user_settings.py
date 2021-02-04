"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from functools import partial
from textwrap import dedent

import pytest

from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.pre_check_user_settings import CheckUserSettingsPlugin
from atomic_reactor.util import df_parser
from atomic_reactor.constants import (CONTAINER_IMAGEBUILDER_BUILD_METHOD,
                                      CONTAINER_DOCKERPY_BUILD_METHOD, REPO_CONTENT_SETS_CONFIG,
                                      REPO_FETCH_ARTIFACTS_URL, REPO_FETCH_ARTIFACTS_KOJI)

from tests.mock_env import MockEnv
from tests.stubs import StubSource

pytestmark = pytest.mark.usefixtures('user_params')


def mock_dockerfile(tmpdir, labels, from_scratch=True):
    base = 'FROM scratch' if from_scratch else 'FROM fedora:30'
    cmd = 'CMD /bin/cowsay moo'
    extra_labels = [
        'LABEL {}'.format(label)
        for label in labels

    ]
    data = '\n'.join([base] + extra_labels + [cmd])
    tmpdir.join('Dockerfile').write(data)


def mock_dockerfile_multistage(tmpdir, labels, from_scratch=False):
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
    tmpdir.join('Dockerfile').write(
        data.format(data_from=data_from, extra_labels=extra_labels)
    )


class FakeSource(StubSource):
    """Fake source for config files validation"""

    def __init__(self, dockerfile_path):
        """Initialize this fake source

        :param dockerfile_path: the path to the dockerfile, not including the dockerfile filename.
        :type dockerfile_path: py.path.LocalPath
        """
        super().__init__()
        self.path = str(dockerfile_path)
        self.dockerfile_path = str(dockerfile_path.join('Dockerfile'))

    def get_build_file_path(self):
        """Ensure the validations run against distgit config files"""
        return self.dockerfile_path, self.path


def mock_env(dockerfile_path, docker_tasker,
             labels=(), flatpak=False, dockerfile_f=mock_dockerfile,
             isolated=None):
    """Mock test environment

    :param dockerfile_path: the path to the fake dockerfile to be created, not including the
        dockerfile filename.
    :type dockerfile_path: py.path.LocalPath
    :param docker_tasker: docker_tasker fixture from conftest. Passed to ``MockEnv.create_runner``
        directly to create a corresponding plugin runner instance.
    :param labels: an iterable labels set for testing operator bundle or appregistry build.
    :type labels: iterable[str]
    :param bool flatpak: a flag to indicate whether the test is for a flatpak build.
    :param callable dockerfile_f: a function to create fake dockerfile. Different test could pass a
        specific function for itself.
    :param bool isolated: a flag to indicated if build is isolated
    """
    if not flatpak:
        # flatpak build has no Dockefile
        dockerfile_f(dockerfile_path, labels)

    env = MockEnv().for_plugin('prebuild', CheckUserSettingsPlugin.key, {'flatpak': flatpak})
    env.workflow.source = FakeSource(dockerfile_path)

    if isolated is not None:
        env.set_isolated(isolated)

    dfp = df_parser(str(dockerfile_path))
    env.workflow.builder.set_df_path(str(dockerfile_path))
    env.workflow.builder.set_dockerfile_images([] if flatpak else dfp.parent_images)

    return env.create_runner(docker_tasker)


class TestDockerfileChecks(object):
    """
    Test checks related to Dockerfile
    """

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
    def test_mutual_exclusivity_of_labels(self, tmpdir, docker_tasker, labels, expected_fail):
        """Appregistry and operator.bundle labels are mutually exclusive"""
        runner = mock_env(tmpdir, docker_tasker, labels=labels)

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
        self, tmpdir, docker_tasker, from_scratch, multistage, labels, expected_fail
    ):
        """Operator bundle can be only single stage and FROM scratch"""
        if multistage:
            dockerfile_f = mock_dockerfile_multistage
        else:
            dockerfile_f = mock_dockerfile

        dockerfile_f = partial(dockerfile_f, from_scratch=from_scratch)

        docker_tasker.build_method = CONTAINER_IMAGEBUILDER_BUILD_METHOD
        runner = mock_env(
            tmpdir, docker_tasker,
            dockerfile_f=dockerfile_f, labels=labels
        )

        if expected_fail:
            with pytest.raises(PluginFailedException) as e:
                runner.run()
            assert 'Operator bundle build can be only' in str(e.value)
        else:
            runner.run()

    def test_flatpak_skip_dockerfile_check(self, tmpdir, docker_tasker, caplog):
        """Flatpak builds have no user Dockerfiles, dockefile check must be skipped"""
        runner = mock_env(tmpdir, docker_tasker, flatpak=True)
        runner.run()

        assert 'Skipping Dockerfile checks' in caplog.text

    @pytest.mark.parametrize(('build_method', 'multistage', 'expected_fail'), [
        (CONTAINER_IMAGEBUILDER_BUILD_METHOD, True, False),
        (CONTAINER_IMAGEBUILDER_BUILD_METHOD, False, False),
        (CONTAINER_DOCKERPY_BUILD_METHOD, True, True),
        (CONTAINER_DOCKERPY_BUILD_METHOD, False, False),
    ])
    def test_multistage_docker_api(self, tmpdir, docker_tasker, build_method, multistage,
                                   expected_fail):
        """Multistage build should fail with docker_api"""
        if multistage:
            dockerfile_f = mock_dockerfile_multistage
        else:
            dockerfile_f = mock_dockerfile

        docker_tasker.build_method = build_method
        runner = mock_env(tmpdir, docker_tasker, dockerfile_f=dockerfile_f)
        if expected_fail:
            with pytest.raises(PluginFailedException) as e:
                runner.run()
            msg = "Multistage builds can't be built with docker_api," \
                  "use 'image_build_method' in container.yaml " \
                  "with '{}'".format(CONTAINER_IMAGEBUILDER_BUILD_METHOD)
            assert msg in str(e.value)

        else:
            runner.run()


def write_fetch_artifacts_url(repo_dir, make_mistake=False):
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
    repo_dir.join(REPO_FETCH_ARTIFACTS_URL).write(content)


def write_fetch_artifacts_koji(repo_dir, make_mistake=False):
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
    repo_dir.join(REPO_FETCH_ARTIFACTS_KOJI).write(content)


def write_content_sets_yml(repo_dir, make_mistake=False):
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
    repo_dir.join(REPO_CONTENT_SETS_CONFIG).write(content)


class TestValidateUserConfigFiles(object):
    """Test the validate_user_config_files"""

    def test_validate_the_config_files(self, docker_tasker, tmpdir):
        write_fetch_artifacts_koji(tmpdir)
        write_fetch_artifacts_url(tmpdir)
        write_content_sets_yml(tmpdir)

        runner = mock_env(tmpdir, docker_tasker)
        runner.run()

    def test_catch_invalid_fetch_artifacts_url(self, docker_tasker, tmpdir):
        write_fetch_artifacts_url(tmpdir, make_mistake=True)

        runner = mock_env(tmpdir, docker_tasker)
        with pytest.raises(PluginFailedException, match="'sha4096' was unexpected"):
            runner.run()

    def test_catch_invalid_fetch_artifacts_koji(self, docker_tasker, tmpdir):
        write_fetch_artifacts_koji(tmpdir, make_mistake=True)

        runner = mock_env(tmpdir, docker_tasker)
        with pytest.raises(PluginFailedException, match="'nvr' is a required property"):
            runner.run()

    def test_catch_invalid_content_sets(self, docker_tasker, tmpdir):
        write_content_sets_yml(tmpdir, make_mistake=True)

        runner = mock_env(tmpdir, docker_tasker)
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
        self, docker_tasker, tmpdir,
        isolated, bundle, from_scratch, expected_fail,
    ):
        """Test if isolated FROM scratch builds are prohibited except
        operator bundle builds"""
        labels = ['com.redhat.delivery.operator.bundle=true'] if bundle else []

        dockerfile_f = partial(mock_dockerfile, from_scratch=from_scratch)

        runner = mock_env(
            tmpdir, docker_tasker,
            dockerfile_f=dockerfile_f, labels=labels, isolated=isolated
        )
        if expected_fail:
            with pytest.raises(PluginFailedException) as exc_info:
                runner.run()

            assert '"FROM scratch" image build cannot be isolated ' in str(exc_info.value)
        else:
            runner.run()
