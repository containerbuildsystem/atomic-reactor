"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import os
import subprocess
import pytest

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.plugins.pre_add_help import AddHelpPlugin
from atomic_reactor.util import ImageName, df_parser
from tests.constants import MOCK_SOURCE
from tests.fixtures import docker_tasker

import atomic_reactor
from tests.test_inner import FakeLogger
from textwrap import dedent
from flexmock import flexmock


class Y(object):
    pass


class X(object):
    image_id = "xxx"
    source = Y()
    source.dockerfile_path = None
    source.path = None
    base_image = ImageName(repo="qwe", tag="asd")


def generate_a_file(destpath, contents):
    with open(destpath, 'w') as f:
        f.write(dedent(contents))


@pytest.mark.parametrize('filename', ['help.md', 'other_file.md'])
def test_add_help_plugin(tmpdir, docker_tasker, filename):
    df_content = dedent("""
        FROM fedora
        RUN yum install -y python-django
        CMD blabla""")
    df = df_parser(str(tmpdir))
    df.content = df_content

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.builder = X
    workflow.builder.df_path = df.dockerfile_path
    workflow.builder.df_dir = str(tmpdir)

    help_markdown_path = os.path.join(workflow.builder.df_dir, filename)
    generate_a_file(help_markdown_path, "foo")
    help_man_path = os.path.join(workflow.builder.df_dir, AddHelpPlugin.man_filename)
    generate_a_file(help_man_path, "bar")

    cmd = ['go-md2man', '-in={}'.format(help_markdown_path), '-out={}'.format(help_man_path)]

    def check_cmd(received_cmd, stderr):
        assert received_cmd == cmd
        assert stderr == subprocess.STDOUT

    (flexmock(subprocess)
         .should_receive("check_output")
         .replace_with(check_cmd))

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AddHelpPlugin.key,
            'args': {'help_file': filename}
        }]
    )
    runner.run()

    assert  df.content == dedent("""
        FROM fedora
        RUN yum install -y python-django
        ADD %s /%s
        CMD blabla""" % (AddHelpPlugin.man_filename, AddHelpPlugin.man_filename))


@pytest.mark.parametrize('filename', ['help.md', 'other_file.md'])
def test_add_help_no_help_file(request, tmpdir, docker_tasker, filename):
    df_content = "FROM fedora"
    df = df_parser(str(tmpdir))
    df.content = df_content

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.builder = X
    workflow.builder.df_path = df.dockerfile_path
    workflow.builder.df_dir = str(tmpdir)

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AddHelpPlugin.key,
            'args': {'help_file': filename}
        }]
    )
    # Runner should not crash if no help.md found
    runner.run()


@pytest.mark.parametrize('filename', ['help.md', 'other_file.md'])
@pytest.mark.parametrize('go_md2man_result', ['binary_missing', 'result_missing', 'fail', 'pass'])
def test_add_help_md2man_error(request, tmpdir, docker_tasker, filename, go_md2man_result):
    df_content = "FROM fedora"
    df = df_parser(str(tmpdir))
    df.content = df_content

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.builder = X
    workflow.builder.df_path = df.dockerfile_path
    workflow.builder.df_dir = str(tmpdir)

    help_markdown_path = os.path.join(workflow.builder.df_dir, filename)
    generate_a_file(help_markdown_path, "foo")
    help_man_path = os.path.join(workflow.builder.df_dir, AddHelpPlugin.man_filename)
    if go_md2man_result != 'result_missing':
        generate_a_file(help_man_path, "bar")

    cmd = [u'go-md2man', u'-in={}'.format(help_markdown_path), u'-out={}'.format(help_man_path)]

    def check_cmd_pass(received_cmd, stderr):
        assert received_cmd == cmd
        assert stderr == subprocess.STDOUT

    def check_cmd_binary_missing(received_cmd, stderr):
        check_cmd_pass(received_cmd, stderr)
        raise subprocess.CalledProcessError(returncode=127, cmd=received_cmd)

    def check_cmd_fail(received_cmd, stderr):
        check_cmd_pass(received_cmd, stderr)
        raise subprocess.CalledProcessError(returncode=1, cmd=received_cmd)


    if go_md2man_result == 'binary_missing':
        (flexmock(subprocess)
             .should_receive("check_output")
             .replace_with(check_cmd_binary_missing))
    elif go_md2man_result == 'fail':
        (flexmock(subprocess)
             .should_receive("check_output")
             .replace_with(check_cmd_fail))
    elif go_md2man_result in ['pass', 'result_missing']:
        (flexmock(subprocess)
             .should_receive("check_output")
             .replace_with(check_cmd_pass))

    fake_logger = FakeLogger()
    existing_logger = atomic_reactor.plugin.logger

    def restore_logger():
        atomic_reactor.plugin.logger = existing_logger

    request.addfinalizer(restore_logger)
    atomic_reactor.plugin.logger = fake_logger

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AddHelpPlugin.key,
            'args': {'help_file': filename}
        }]
    )

    runner.run()

    if go_md2man_result == 'binary_missing':
        error_message = "Help file is available, but go-md2man is not present in a buildroot"
    elif go_md2man_result == 'fail':
        error_message = "CalledProcessError()"
    elif go_md2man_result == 'result_missing':
        error_message = "go-md2man run complete, but man file is not found"

    if go_md2man_result != 'pass':
        # Python 2 prints the error message as "RuntimeError(u'...", but not py3
        # The test checks for correct plugin and error message text in two passes
        assert "plugin 'add_help' raised an exception: RuntimeError" in fake_logger.warnings[-1][0]
        assert error_message in fake_logger.warnings[-1][0]
