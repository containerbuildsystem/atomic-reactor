"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

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


class MockedPopen(object):
    def __init__(self, *args, **kwargs):
        self.args = []

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        pass

    def poll(self):
        pass

    def communicate(self, input=None, timeout=None):
        return ('', '')


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

    def check_popen(*args, **kwargs):
        assert args[0] == cmd
        return MockedPopen()

    (flexmock(subprocess)
         .should_receive("Popen")
         .once()
         .replace_with(check_popen))

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
    result = runner.run()
    assert result == {'add_help': {
        'status': AddHelpPlugin.NO_HELP_FILE_FOUND,
        'help_file': filename
    }}


@pytest.mark.parametrize('filename', ['help.md', 'other_file.md'])
@pytest.mark.parametrize('go_md2man_result', [
    'binary_missing', 'input_missing', 'other_os_error',
    'result_missing', 'fail', 'pass'])
def test_add_help_md2man_error(request, tmpdir, docker_tasker, filename,
                               go_md2man_result, caplog):
    df_content = "FROM fedora"
    df = df_parser(str(tmpdir))
    df.content = df_content

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    workflow.builder = X
    workflow.builder.df_path = df.dockerfile_path
    workflow.builder.df_dir = str(tmpdir)

    help_markdown_path = os.path.join(workflow.builder.df_dir, filename)
    if go_md2man_result != 'input_missing':
        generate_a_file(help_markdown_path, "foo")
    help_man_path = os.path.join(workflow.builder.df_dir, AddHelpPlugin.man_filename)
    if go_md2man_result != 'result_missing':
        generate_a_file(help_man_path, "bar")

    cmd = ['go-md2man',
           '-in={}'.format(help_markdown_path),
           '-out={}'.format(help_man_path)]

    def check_popen_pass(*args, **kwargs):
        assert args[0] == cmd
        return MockedPopen()

    def check_popen_binary_missing(*args, **kwargs):
        check_popen_pass(*args, **kwargs)
        raise OSError(2, "No such file or directory")

    def check_popen_other_os_error(*args, **kwargs):
        check_popen_pass(*args, **kwargs)
        raise OSError(0, "Other error")

    def check_popen_fail(*args, **kwargs):
        check_popen_pass(*args, **kwargs)
        raise subprocess.CalledProcessError(returncode=1, cmd=args[0])


    if go_md2man_result == 'binary_missing':
        (flexmock(subprocess)
             .should_receive("Popen")
             .once()
             .replace_with(check_popen_binary_missing))
    elif go_md2man_result == 'other_os_error':
        (flexmock(subprocess)
             .should_receive("Popen")
             .once()
             .replace_with(check_popen_other_os_error))
    elif go_md2man_result == 'fail':
        (flexmock(subprocess)
             .should_receive("Popen")
             .once()
             .replace_with(check_popen_fail))
    elif go_md2man_result in ['pass', 'result_missing']:
        (flexmock(subprocess)
             .should_receive("Popen")
             .once()
             .replace_with(check_popen_pass))

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AddHelpPlugin.key,
            'args': {'help_file': filename}
        }]
    )

    result = runner.run()

    if go_md2man_result == 'binary_missing':
        assert list(result.keys()) == ['add_help']
        assert isinstance(result['add_help'], RuntimeError)
        assert 'Help file is available, but go-md2man is not present in a buildroot' \
            == str(result['add_help'])

    elif go_md2man_result == 'other_os_error':
        assert list(result.keys()) == ['add_help']
        assert isinstance(result['add_help'], OSError)


    elif go_md2man_result == 'result_missing':
        assert list(result.keys()) == ['add_help']
        assert isinstance(result['add_help'], RuntimeError)
        assert 'go-md2man run complete, but man file is not found' == str(result['add_help'])

    elif go_md2man_result == 'input_missing':
        expected_result = {
            'add_help': {
                'status': AddHelpPlugin.NO_HELP_FILE_FOUND,
                'help_file': filename
            }
        }
        assert result == expected_result

    elif go_md2man_result == 'pass':
        expected_result = {
            'add_help': {
                'status': AddHelpPlugin.HELP_GENERATED,
                'help_file': filename
            }
        }
        assert result == expected_result

    elif go_md2man_result == 'fail':
        assert list(result.keys()) == ['add_help']
        assert isinstance(result['add_help'], RuntimeError)
        assert 'Error running' in str(result['add_help'])
