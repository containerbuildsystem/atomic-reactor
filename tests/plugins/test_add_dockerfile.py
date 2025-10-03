"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from pathlib import Path
from typing import Dict, Optional, NamedTuple

from atomic_reactor.plugin import PluginsRunner
from atomic_reactor.plugins.add_dockerfile import AddDockerfilePlugin
from atomic_reactor.constants import DOCKERIGNORE, DOCKERFILE_FILENAME

from tests.mock_env import MockEnv


DOCKERIGNORE_CONTENT = """
Dockerfile*
*.json
*.other"""


def mock_env(
    workflow, df_content: str, args: Optional[Dict[str, str]] = None,
    dockerignore: Optional[bool] = False
) -> PluginsRunner:
    env = MockEnv(workflow).for_plugin(AddDockerfilePlugin.key, args)

    (Path(workflow.source.path) / "Dockerfile").write_text(df_content)

    if dockerignore:
        (Path(workflow.source.path) / DOCKERIGNORE).write_text(DOCKERIGNORE_CONTENT)

    workflow.build_dir.init_build_dirs(["aarch64", "x86_64"], workflow.source)

    return env.create_runner()


class DockerfileCopy(NamedTuple):
    name: str
    content: str


def check_outputs(expected_df_content: str, expected_df_copy: Optional[DockerfileCopy] = None):
    def check_in_build_dir(build_dir):
        df_content = build_dir.dockerfile_path.read_text()
        assert df_content == expected_df_content

        if expected_df_copy:
            df_copy_path = build_dir.path / expected_df_copy.name
            assert df_copy_path.read_text() == expected_df_copy.content

    return check_in_build_dir


def check_dockerignore(nvr: str):
    def check_in_build_dir(build_dir):
        dockerignore = build_dir.path / DOCKERIGNORE
        dockerignore_content = dockerignore.read_text()

        final_content = DOCKERIGNORE_CONTENT
        final_content += f"\n!{DOCKERFILE_FILENAME}-{nvr}\n"
        assert dockerignore_content == final_content

    return check_in_build_dir


def test_adddockerfile_plugin(tmpdir, workflow):  # noqa
    df_content = """
FROM fedora
RUN yum install -y python-django
CMD blabla"""

    runner = mock_env(workflow, df_content, {'nvr': 'rhel-server-docker-7.1-20'})
    runner.run()

    assert AddDockerfilePlugin.key is not None

    expected_df_content = """
FROM fedora
RUN yum install -y python-django
COPY Dockerfile-rhel-server-docker-7.1-20 /root/buildinfo/Dockerfile-rhel-server-docker-7.1-20
CMD blabla"""
    # the copied Dockerfile should have the *original* content
    expected_df_copy = DockerfileCopy("Dockerfile-rhel-server-docker-7.1-20", df_content)

    workflow.build_dir.for_each_platform(check_outputs(expected_df_content, expected_df_copy))


def test_adddockerfile_todest(tmpdir, workflow):  # noqa
    df_content = """
FROM fedora
RUN yum install -y python-django
CMD blabla"""

    runner = mock_env(
        workflow, df_content, {'nvr': 'jboss-eap-6-docker-6.4-77', 'destdir': '/usr/share/doc/'}
    )
    runner.run()

    assert AddDockerfilePlugin.key is not None

    expected_df_content = """
FROM fedora
RUN yum install -y python-django
COPY Dockerfile-jboss-eap-6-docker-6.4-77 /usr/share/doc/Dockerfile-jboss-eap-6-docker-6.4-77
CMD blabla"""
    expected_df_copy = DockerfileCopy("Dockerfile-jboss-eap-6-docker-6.4-77", df_content)

    workflow.build_dir.for_each_platform(check_outputs(expected_df_content, expected_df_copy))


def test_adddockerfile_nvr_from_labels(tmpdir, workflow):  # noqa
    df_content = """
FROM fedora
RUN yum install -y python-django
LABEL Name="jboss-eap-6-docker" "Version"="6.4" "Release"=77
CMD blabla"""

    runner = mock_env(workflow, df_content)
    runner.run()

    assert AddDockerfilePlugin.key is not None

    expected_df_content = """
FROM fedora
RUN yum install -y python-django
LABEL Name="jboss-eap-6-docker" "Version"="6.4" "Release"=77
COPY Dockerfile-jboss-eap-6-docker-6.4-77 /root/buildinfo/Dockerfile-jboss-eap-6-docker-6.4-77
CMD blabla"""
    expected_df_copy = DockerfileCopy("Dockerfile-jboss-eap-6-docker-6.4-77", df_content)

    workflow.build_dir.for_each_platform(check_outputs(expected_df_content, expected_df_copy))


def test_adddockerfile_fails(tmpdir, caplog, workflow):  # noqa
    df_content = """
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    runner = mock_env(workflow, df_content)
    runner.run()
    assert "plugin 'add_dockerfile' raised an exception: ValueError" in caplog.text


def test_adddockerfile_final(tmpdir, workflow):  # noqa
    df_content = """
FROM fedora
RUN yum install -y python-django
CMD blabla"""

    runner = mock_env(
        workflow, df_content, {'nvr': 'rhel-server-docker-7.1-20', "use_final_dockerfile": True}
    )
    runner.run()

    assert AddDockerfilePlugin.key is not None

    expected_df_content = """
FROM fedora
RUN yum install -y python-django
COPY Dockerfile /root/buildinfo/Dockerfile-rhel-server-docker-7.1-20
CMD blabla"""
    workflow.build_dir.for_each_platform(check_outputs(expected_df_content))


def test_adddockerfile_plugin_edit_dockerignore(workflow):  # noqa
    df_content = """
FROM fedora
RUN yum install -y python-django
CMD blabla"""

    nvr = 'rhel-server-docker-7.1-20'
    runner = mock_env(workflow, df_content, {'nvr': nvr},
                      dockerignore=True)
    runner.run()

    assert AddDockerfilePlugin.key is not None

    workflow.build_dir.for_each_platform(check_dockerignore(nvr))
