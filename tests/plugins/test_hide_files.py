# -*- coding: utf-8 -*-
"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import print_function, unicode_literals, absolute_import

import os
from textwrap import dedent

import pytest

from atomic_reactor.constants import INSPECT_CONFIG
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin, ReactorConfig,
                                                       WORKSPACE_CONF_KEY)
from atomic_reactor.plugins.pre_hide_files import HideFilesPlugin
from atomic_reactor.util import df_parser

from tests.constants import SOURCE, MOCK
from tests.stubs import StubInsideBuilder


if MOCK:
    from tests.docker_mock import mock_docker


class TestHideFilesPlugin(object):

    def test_missing_config(self, tmpdir):
        df_content = dedent("""\
            FROM fedora
            RUN yum install -y python-flask
            CMD /bin/bash
            """)
        df = df_parser(str(tmpdir))
        df.content = df_content

        tasker, workflow = self.prepare(df.dockerfile_path)

        runner = PreBuildPluginsRunner(tasker, workflow, [
            {'name': HideFilesPlugin.key, 'args': {}},
        ])
        runner_results = runner.run()

        assert runner_results[HideFilesPlugin.key] is None
        # Verify Dockerfile contents have not changed
        assert df.content == df_content

    @pytest.mark.parametrize(('df_content', 'expected_df', 'inherited_user'), [
        (
            dedent("""\
                FROM sha256:123456
                RUN yum install -y python-flask
                CMD /bin/bash
            """),
            dedent("""\
                FROM sha256:123456
                RUN mv -f /etc/yum.repos.d/repo_ignore_1.repo /tmp || :
                RUN mv -f /etc/yum.repos.d/repo_ignore_2.repo /tmp || :
                RUN yum install -y python-flask
                CMD /bin/bash
                RUN mv -fZ /tmp/repo_ignore_1.repo /etc/yum.repos.d/repo_ignore_1.repo || :
                RUN mv -fZ /tmp/repo_ignore_2.repo /etc/yum.repos.d/repo_ignore_2.repo || :
            """),
            None
        ),

        (
            dedent("""\
                FROM sha256:123456
                RUN yum install -y python-flask
                USER custom_docker_user
                CMD /bin/bash
            """),
            dedent("""\
                FROM sha256:123456
                RUN mv -f /etc/yum.repos.d/repo_ignore_1.repo /tmp || :
                RUN mv -f /etc/yum.repos.d/repo_ignore_2.repo /tmp || :
                RUN yum install -y python-flask
                USER custom_docker_user
                CMD /bin/bash
                USER root
                RUN mv -fZ /tmp/repo_ignore_1.repo /etc/yum.repos.d/repo_ignore_1.repo || :
                RUN mv -fZ /tmp/repo_ignore_2.repo /etc/yum.repos.d/repo_ignore_2.repo || :
                USER custom_docker_user
            """),
            None
        ),

        (
            dedent("""\
                FROM sha256:123456
                RUN yum install -y python-flask
                CMD /bin/bash
            """),
            dedent("""\
                FROM sha256:123456
                USER root
                RUN mv -f /etc/yum.repos.d/repo_ignore_1.repo /tmp || :
                RUN mv -f /etc/yum.repos.d/repo_ignore_2.repo /tmp || :
                USER inherited_user
                RUN yum install -y python-flask
                CMD /bin/bash
                USER root
                RUN mv -fZ /tmp/repo_ignore_1.repo /etc/yum.repos.d/repo_ignore_1.repo || :
                RUN mv -fZ /tmp/repo_ignore_2.repo /etc/yum.repos.d/repo_ignore_2.repo || :
                USER inherited_user
            """),
            "inherited_user"
        ),

        (
            dedent("""\
                FROM sha256:123456
                RUN yum install -y python-flask
                USER custom_docker_user
                CMD /bin/bash
            """),
            dedent("""\
                FROM sha256:123456
                USER root
                RUN mv -f /etc/yum.repos.d/repo_ignore_1.repo /tmp || :
                RUN mv -f /etc/yum.repos.d/repo_ignore_2.repo /tmp || :
                USER inherited_user
                RUN yum install -y python-flask
                USER custom_docker_user
                CMD /bin/bash
                USER root
                RUN mv -fZ /tmp/repo_ignore_1.repo /etc/yum.repos.d/repo_ignore_1.repo || :
                RUN mv -fZ /tmp/repo_ignore_2.repo /etc/yum.repos.d/repo_ignore_2.repo || :
                USER custom_docker_user
            """),
            "inherited_user"
        ),

        (
            dedent("""\
                FROM scratch
                RUN yum install -y python-flask
                USER custom_docker_user
                CMD /bin/bash
            """),
            dedent("""\
                FROM scratch
                RUN yum install -y python-flask
                USER custom_docker_user
                CMD /bin/bash
            """),
            "inherited_user"
        ),
    ])
    def test_hide_files(self, tmpdir, df_content, expected_df, inherited_user):
        df = df_parser(str(tmpdir))
        df.content = df_content
        hide_files = {'tmpdir': '/tmp', 'files': ['/etc/yum.repos.d/repo_ignore_1.repo',
                                                  '/etc/yum.repos.d/repo_ignore_2.repo']}
        parent_images = [
            'sha256:123456',
        ]

        tasker, workflow = self.prepare(
            df.dockerfile_path, hide_files=hide_files, parent_images=parent_images,
            inherited_user=inherited_user)

        runner = PreBuildPluginsRunner(tasker, workflow, [
            {'name': HideFilesPlugin.key, 'args': {}},
        ])
        runner.run()

        assert df.content == expected_df

    def test_hide_files_multi_stage(self, tmpdir):
        df_content = dedent("""\
            FROM sha256:123456 as builder
            RUN blah
            USER custom_user
            RUN bluh

            FROM sha256:654321 as unused
            RUN bleh

            FROM sha256:123456
            RUN yum install -y python-flask
            USER custom_user2
            CMD /bin/bash
            """)
        df = df_parser(str(tmpdir))
        df.content = df_content
        hide_files = {'tmpdir': '/tmp', 'files': ['/etc/yum.repos.d/repo_ignore_1.repo',
                                                  '/etc/yum.repos.d/repo_ignore_2.repo']}
        parent_images = [
            'sha256:123456',
            'sha256:654321',
        ]

        tasker, workflow = self.prepare(
            df.dockerfile_path, hide_files=hide_files, parent_images=parent_images,
            inherited_user="inherited_user")

        runner = PreBuildPluginsRunner(tasker, workflow, [
            {'name': HideFilesPlugin.key, 'args': {}},
        ])
        runner.run()

        expected_df_content = dedent("""\
            FROM sha256:123456 as builder
            USER root
            RUN mv -f /etc/yum.repos.d/repo_ignore_1.repo /tmp || :
            RUN mv -f /etc/yum.repos.d/repo_ignore_2.repo /tmp || :
            USER inherited_user
            RUN blah
            USER custom_user
            RUN bluh
            USER root
            RUN mv -fZ /tmp/repo_ignore_1.repo /etc/yum.repos.d/repo_ignore_1.repo || :
            RUN mv -fZ /tmp/repo_ignore_2.repo /etc/yum.repos.d/repo_ignore_2.repo || :
            USER custom_user

            FROM sha256:654321 as unused
            USER root
            RUN mv -f /etc/yum.repos.d/repo_ignore_1.repo /tmp || :
            RUN mv -f /etc/yum.repos.d/repo_ignore_2.repo /tmp || :
            USER inherited_user
            RUN bleh
            USER root
            RUN mv -fZ /tmp/repo_ignore_1.repo /etc/yum.repos.d/repo_ignore_1.repo || :
            RUN mv -fZ /tmp/repo_ignore_2.repo /etc/yum.repos.d/repo_ignore_2.repo || :
            USER inherited_user

            FROM sha256:123456
            USER root
            RUN mv -f /etc/yum.repos.d/repo_ignore_1.repo /tmp || :
            RUN mv -f /etc/yum.repos.d/repo_ignore_2.repo /tmp || :
            USER inherited_user
            RUN yum install -y python-flask
            USER custom_user2
            CMD /bin/bash
            USER root
            RUN mv -fZ /tmp/repo_ignore_1.repo /etc/yum.repos.d/repo_ignore_1.repo || :
            RUN mv -fZ /tmp/repo_ignore_2.repo /etc/yum.repos.d/repo_ignore_2.repo || :
            USER custom_user2
            """)
        assert df.content == expected_df_content

    def prepare(self, df_path, inherited_user='', hide_files=None, parent_images=None):
        if MOCK:
            mock_docker()
        tasker = DockerTasker()
        workflow = DockerBuildWorkflow("test-image", source=SOURCE)
        workflow.source = MockSource(df_path)
        workflow.builder = (StubInsideBuilder()
                            .for_workflow(workflow)
                            .set_df_path(df_path))

        for parent in parent_images or []:
            workflow.builder.set_parent_inspection_data(parent, {
                INSPECT_CONFIG: {
                    'User': inherited_user,
                },
            })

        if hide_files is not None:
            reactor_config = ReactorConfig({
                'version': 1,
                'hide_files': hide_files
            })
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {
                WORKSPACE_CONF_KEY: reactor_config
            }

        return tasker, workflow


class MockSource(object):
    def __init__(self, dockerfile_path):
        self.dockerfile_path = dockerfile_path
        self.path = os.path.dirname(dockerfile_path)

    def get_build_file_path(self):
        return self.dockerfile_path, self.path

    @property
    def workdir(self):
        return self.path
