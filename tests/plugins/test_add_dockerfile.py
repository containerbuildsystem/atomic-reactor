"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import pytest
from dock.core import DockerTasker
from dock.inner import DockerBuildWorkflow
from dock.plugin import PreBuildPluginsRunner
from dock.plugins.pre_add_dockerfile import AddDockerfilePlugin
from dock.plugins.pre_add_labels_in_df import AddLabelsPlugin
from dock.util import ImageName, DockerfileParser



class X(object):
    image_id = "xxx"
    git_dockerfile_path = None
    git_path = None
    base_image = ImageName(repo="qwe", tag="asd")


def test_adddockerfile_plugin(tmpdir):
    df_content = """
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow('asd', 'test-image')
    workflow.builder = X
    workflow.builder.df_path = df.dockerfile_path
    workflow.builder.df_dir = str(tmpdir)

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': AddDockerfilePlugin.key,
            'args': {'nvr': 'rhel-server-docker-7.1-20'}
        }]
    )
    runner.run()
    assert AddDockerfilePlugin.key is not None

    expected_output = """
FROM fedora
RUN yum install -y python-django
ADD Dockerfile-rhel-server-docker-7.1-20 /root/buildinfo/Dockerfile-rhel-server-docker-7.1-20
CMD blabla"""
    assert df.content == expected_output


def test_adddockerfile_todest(tmpdir):
    df_content = """
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow('asd', 'test-image')
    workflow.builder = X
    workflow.builder.df_path = df.dockerfile_path
    workflow.builder.df_dir = str(tmpdir)

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': AddDockerfilePlugin.key,
            'args': {'nvr': 'jboss-eap-6-docker-6.4-77',
                     'destdir': '/usr/share/doc/'}
        }]
    )
    runner.run()
    assert AddDockerfilePlugin.key is not None

    expected_output = """
FROM fedora
RUN yum install -y python-django
ADD Dockerfile-jboss-eap-6-docker-6.4-77 /usr/share/doc/Dockerfile-jboss-eap-6-docker-6.4-77
CMD blabla"""
    assert df.content == expected_output


def test_adddockerfile_nvr_from_labels(tmpdir):
    df_content = """
FROM fedora
RUN yum install -y python-django
LABEL Name="jboss-eap-6-docker" "Version"="6.4" "Release"=77
CMD blabla"""
    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow('asd', 'test-image')
    workflow.builder = X
    workflow.builder.df_path = df.dockerfile_path
    workflow.builder.df_dir = str(tmpdir)

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': AddDockerfilePlugin.key
        }]
    )
    runner.run()
    assert AddDockerfilePlugin.key is not None

    assert "ADD Dockerfile-jboss-eap-6-docker-6.4-77 /root/buildinfo/Dockerfile-jboss-eap-6-docker-6.4-77" in df.content


def test_adddockerfile_nvr_from_labels2(tmpdir):
    df_content = """
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow('asd', 'test-image')
    workflow.builder = X
    workflow.builder.df_path = df.dockerfile_path
    workflow.builder.df_dir = str(tmpdir)

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': AddLabelsPlugin.key,
            'args': {'labels': {'Name': 'jboss-eap-6-docker',
                                'Version': '6.4',
                                'Release': '77'}}
         },
         {
            'name': AddDockerfilePlugin.key
        }]
    )
    runner.run()
    assert AddDockerfilePlugin.key is not None

    assert "ADD Dockerfile-jboss-eap-6-docker-6.4-77 /root/buildinfo/Dockerfile-jboss-eap-6-docker-6.4-77" in df.content


def test_adddockerfile_fails(tmpdir):
    df_content = """
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow('asd', 'test-image')
    workflow.builder = X
    workflow.builder.df_path = df.dockerfile_path
    workflow.builder.df_dir = str(tmpdir)

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': AddDockerfilePlugin.key
        }]
    )
    with pytest.raises(ValueError):
        runner.run()


def test_adddockerfile_final(tmpdir):
    df_content = """
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow('asd', 'test-image')
    workflow.builder = X
    workflow.builder.df_path = df.dockerfile_path
    workflow.builder.df_dir = str(tmpdir)

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
             'name': AddDockerfilePlugin.key,
             'args': {'nvr': 'rhel-server-docker-7.1-20', "use_final_dockerfile": True}
        }]
    )
    runner.run()
    assert AddDockerfilePlugin.key is not None

    expected_output = """
FROM fedora
RUN yum install -y python-django
ADD Dockerfile /root/buildinfo/Dockerfile-rhel-server-docker-7.1-20
CMD blabla"""
    assert df.content == expected_output

