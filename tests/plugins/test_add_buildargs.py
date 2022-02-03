# -*- coding: utf-8 -*-
"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from pathlib import Path
from textwrap import dedent
from typing import Dict

import pytest

from atomic_reactor.plugin import PluginsRunner
from atomic_reactor.plugins.pre_add_buildargs_in_df import AddBuildargsPlugin

from tests.mock_env import MockEnv


def mock_env(workflow, df_content: str, buildargs: Dict[str, str]) -> PluginsRunner:
    env = MockEnv(workflow).for_plugin("prebuild", AddBuildargsPlugin.key)
    (Path(workflow.source.path) / "Dockerfile").write_text(df_content)
    workflow.build_dir.init_build_dirs(["aarch64", "x86_64"], workflow.source)
    workflow.data.buildargs = buildargs
    return env.create_runner()


@pytest.mark.parametrize('buildargs, df_content, df_expected', [
    (
        None,
        dedent("""\
            FROM base_image
            RUN yum install -y apache
            CMD blabla
        """),
        dedent("""\
            FROM base_image
            RUN yum install -y apache
            CMD blabla
        """)
    ),

    (
        {},
        dedent("""\
            FROM base_image
            RUN yum install -y apache
            CMD blabla
        """),
        dedent("""\
            FROM base_image
            RUN yum install -y apache
            CMD blabla
        """)
    ),

    (
        {'arg1': 'val1'},
        dedent("""\
            FROM base_image
            RUN yum install -y apache
            CMD blabla
        """),
        dedent("""\
            FROM base_image
            ARG arg1
            RUN yum install -y apache
            CMD blabla
        """)
    ),

    (
        {'arg1': 'val1', 'arg2': 'val2'},
        dedent("""\
            FROM base_image
            RUN yum install -y apache
            CMD blabla
        """),
        dedent("""\
            FROM base_image
            ARG arg1
            ARG arg2
            RUN yum install -y apache
            CMD blabla
        """)
    ),

    (
        {},
        dedent("""\
            FROM base_image
            RUN yum install -y apache
            CMD blabla

            FROM base_image_2
            RUN yum isntall -y vim
            CMD blabla2
        """),
        dedent("""\
            FROM base_image
            RUN yum install -y apache
            CMD blabla

            FROM base_image_2
            RUN yum isntall -y vim
            CMD blabla2
        """)
    ),

    (
        {'arg1': 'val1', 'arg2': 'val2'},
        dedent("""\
            FROM base_image
            RUN yum install -y apache
            CMD blabla

            FROM base_image_2
            RUN yum isntall -y vim
            CMD blabla2
        """),
        dedent("""\
            FROM base_image
            ARG arg1
            ARG arg2
            RUN yum install -y apache
            CMD blabla

            FROM base_image_2
            ARG arg1
            ARG arg2
            RUN yum isntall -y vim
            CMD blabla2
        """)
    ),
])
def test_add_buildargs_plugin(workflow, caplog, buildargs, df_content, df_expected):
    runner = mock_env(workflow, df_content, buildargs)
    runner.run()

    def check_df(build_dir):
        assert build_dir.dockerfile_path.read_text() == df_expected

    workflow.build_dir.for_each_platform(check_df)

    if not buildargs:
        assert 'No buildargs specified, skipping plugin' in caplog.text
