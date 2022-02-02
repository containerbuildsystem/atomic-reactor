# -*- coding: utf-8 -*-
"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from pathlib import Path
from textwrap import dedent

import pytest
from flexmock import flexmock

from atomic_reactor.constants import INSPECT_CONFIG
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.plugins.pre_hide_files import HideFilesPlugin


def check_df_content(expected_content, workflow):
    def check_in_build_dir(build_dir):
        assert build_dir.dockerfile_path.read_text() == expected_content

    workflow.build_dir.for_each_platform(check_in_build_dir)


@pytest.mark.usefixtures('user_params')
class TestHideFilesPlugin(object):

    def test_missing_config(self, workflow):
        df_content = dedent("""\
            FROM fedora
            RUN yum install -y python-flask
            CMD /bin/bash
            """)

        self.prepare(workflow, df_content)

        runner = PreBuildPluginsRunner(workflow, [
            {'name': HideFilesPlugin.key, 'args': {}},
        ])
        runner_results = runner.run()

        assert runner_results[HideFilesPlugin.key] is None
        # Verify Dockerfile contents have not changed
        check_df_content(df_content, workflow)

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
    def test_hide_files(self, workflow, df_content, expected_df, inherited_user):
        hide_files = {'tmpdir': '/tmp', 'files': ['/etc/yum.repos.d/repo_ignore_1.repo',
                                                  '/etc/yum.repos.d/repo_ignore_2.repo']}
        parent_images = [
            'sha256:123456',
        ]

        self.prepare(workflow,
                     df_content,
                     hide_files=hide_files,
                     parent_images=parent_images,
                     inherited_user=inherited_user)

        runner = PreBuildPluginsRunner(workflow, [
            {'name': HideFilesPlugin.key, 'args': {}},
        ])
        runner.run()

        check_df_content(expected_df, workflow)

    def test_hide_files_multi_stage(self, workflow):
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
        hide_files = {'tmpdir': '/tmp', 'files': ['/etc/yum.repos.d/repo_ignore_1.repo',
                                                  '/etc/yum.repos.d/repo_ignore_2.repo']}
        parent_images = [
            'sha256:123456',
            'sha256:654321',
        ]

        self.prepare(workflow,
                     df_content,
                     hide_files=hide_files,
                     parent_images=parent_images,
                     inherited_user="inherited_user")

        runner = PreBuildPluginsRunner(workflow, [
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
        check_df_content(expected_df_content, workflow)

    def prepare(self, workflow, df_content, inherited_user='', hide_files=None, parent_images=None):
        (Path(workflow.source.path) / "Dockerfile").write_text(df_content)
        workflow.build_dir.init_build_dirs(["aarch64", "x86_64"], workflow.source)

        for parent in parent_images or []:
            (flexmock(workflow.imageutil)
             .should_receive('get_inspect_for_image')
             .with_args(parent)
             .and_return({INSPECT_CONFIG: {'User': inherited_user}}))

        if hide_files is not None:
            reactor_config = {'version': 1, 'hide_files': hide_files}
            workflow.conf.conf = reactor_config
