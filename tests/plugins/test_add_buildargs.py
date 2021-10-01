# -*- coding: utf-8 -*-
"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from textwrap import dedent
import pytest
from flexmock import flexmock
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.plugins.pre_add_buildargs_in_df import (
    AddBuildargsPlugin)
from atomic_reactor.util import df_parser
from tests.stubs import StubSource


def prepare(df_path):
    workflow = DockerBuildWorkflow(source=None)
    workflow.source = StubSource()
    flexmock(workflow, df_path=df_path)

    return workflow


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
def test_add_buildargs_plugin(tmpdir, caplog, user_params, buildargs, df_content, df_expected):
    df = df_parser(str(tmpdir))
    df.content = df_content

    workflow = prepare(df.dockerfile_path)
    workflow.buildargs = buildargs

    runner = PreBuildPluginsRunner(workflow, [{
        'name': AddBuildargsPlugin.key,
        'args': {}
    }])
    runner.run()

    assert df_expected == df.content

    if not buildargs:
        assert 'No buildargs specified, skipping plugin' in caplog.text
