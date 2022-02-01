"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import subprocess
import pytest
from datetime import datetime as dt
from pathlib import Path
from textwrap import dedent
from typing import Dict, Optional, Callable, NamedTuple, List

from flexmock import flexmock

from atomic_reactor.plugin import PluginsRunner
from atomic_reactor.plugins.pre_add_help import AddHelpPlugin
from atomic_reactor import start_time as atomic_reactor_start_time

from tests.mock_env import MockEnv


class MockedPopen(object):
    def __init__(self, *args, **kwargs):
        self.args = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, value, traceback):
        pass

    def poll(self):
        pass

    def communicate(self, input=None, timeout=None):    # pylint: disable=redefined-builtin
        return ('', '')


class MD2ManArgs(NamedTuple):
    in_path: Path
    out_path: Path


def parse_md2man_args(cmd: List[str]) -> MD2ManArgs:
    in_opt = "-in="
    out_opt = "-out="

    assert len(cmd) == 3
    assert cmd[0] == "go-md2man"
    assert cmd[1].startswith(in_opt)
    assert cmd[2].startswith(out_opt)

    in_path = cmd[1][len(in_opt):]
    out_path = cmd[2][len(out_opt):]
    return MD2ManArgs(Path(in_path), Path(out_path))


class HelpMdFile(NamedTuple):
    name: str
    content: str


def mock_md2man_success(out_content: str) -> Callable[..., MockedPopen]:
    def mocked_md2man(*args, **kwargs):
        cmd = args[0]
        _, out_path = parse_md2man_args(cmd)
        out_path.write_text(out_content)
        return MockedPopen()

    return mocked_md2man


def mock_env(
    workflow,
    *,
    df_content: str,
    help_md: Optional[HelpMdFile] = None,
    plugin_args: Optional[Dict[str, str]] = None,
    mock_md2man: Callable[..., MockedPopen] = mock_md2man_success("man file content"),
) -> PluginsRunner:

    env = MockEnv(workflow).for_plugin("prebuild", AddHelpPlugin.key, plugin_args)
    source_dir = Path(workflow.source.path)

    (source_dir / "Dockerfile").write_text(df_content)
    if help_md:
        (source_dir / help_md.name).write_text(help_md.content)

    workflow.build_dir.init_build_dirs(["aarch64", "x86_64"], workflow.source)

    if help_md:
        def check_popen(*args, **kwargs):
            cmd = args[0]
            in_path, out_path = parse_md2man_args(cmd)

            assert in_path.name == help_md.name
            assert out_path.name == AddHelpPlugin.man_filename

            return mock_md2man(*args, **kwargs)

        (flexmock(subprocess)
         .should_receive("Popen")
         .once()
         .replace_with(check_popen))
    else:
        (flexmock(subprocess)
         .should_receive("Popen")
         .never())

    return env.create_runner()


@pytest.mark.parametrize('filename', ['help.md', 'other_file.md'])  # noqa
def test_add_help_plugin(workflow, filename):
    df_content = dedent(
        """
        FROM fedora
        RUN yum install -y python-django
        CMD blabla
        """
    )

    runner = mock_env(
        workflow,
        df_content=df_content,
        help_md=HelpMdFile(filename, "markdown file content"),
        plugin_args={"help_file": filename},
        mock_md2man=mock_md2man_success("man file content")
    )
    runner.run()

    def check_df_and_man_file(build_dir):
        assert build_dir.dockerfile_path.read_text() == dedent(
            f"""
            FROM fedora
            RUN yum install -y python-django
            ADD {AddHelpPlugin.man_filename} /{AddHelpPlugin.man_filename}
            CMD blabla
            """
        )
        assert (build_dir.path / AddHelpPlugin.man_filename).read_text() == "man file content"

    workflow.build_dir.for_each_platform(check_df_and_man_file)
    assert workflow.annotations['help_file'] == filename


@pytest.mark.parametrize('filename', ['help.md', 'other_file.md'])  # noqa
def test_add_help_no_help_file(workflow, filename):
    runner = mock_env(workflow, df_content="FROM fedora")
    # Runner should not crash if no help.md found
    result = runner.run()
    assert result == {'add_help': {
        'status': AddHelpPlugin.NO_HELP_FILE_FOUND,
        'help_file': None
    }}


@pytest.mark.parametrize('filename', ['help.md', 'other_file.md'])  # noqa
@pytest.mark.parametrize('go_md2man_result', [
    'binary_missing', 'input_missing', 'other_os_error',
    'result_missing', 'fail', 'pass'])
def test_add_help_md2man_error(workflow, filename, go_md2man_result):
    if go_md2man_result != 'input_missing':
        help_md = HelpMdFile(filename, "markdown file content")
    else:
        help_md = None

    def md2man_binary_missing(*args, **kwargs):
        raise OSError(2, "No such file or directory")

    def md2man_other_os_error(*args, **kwargs):
        raise OSError(0, "Other error")

    def md2man_fail(*args, **kwargs):
        raise subprocess.CalledProcessError(returncode=1, cmd=args[0])

    def md2man_no_output(*args, **kwargs):
        return MockedPopen()

    runner = mock_env(
        workflow,
        df_content="FROM fedora",
        help_md=help_md,
        plugin_args={"help_file": filename},
        mock_md2man=(
            md2man_binary_missing if go_md2man_result == "binary_missing"
            else md2man_other_os_error if go_md2man_result == "other_os_error"
            else md2man_fail if go_md2man_result == "fail"
            else md2man_no_output if go_md2man_result == "result_missing"
            else mock_md2man_success("man file content")
        )
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
                'help_file': None
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


@pytest.mark.parametrize('filename', ['help.md', 'other_file.md'])  # noqa
def test_add_help_generate_metadata(workflow, filename):
    df_content = dedent("""\
        FROM fedora
        LABEL name='test' \\
              maintainer='me'
        """)

    runner = mock_env(
        workflow,
        df_content=df_content,
        help_md=HelpMdFile(filename, "markdown file content\n"),
        plugin_args={"help_file": filename},
    )
    runner.run()

    expect_content = dedent(
        f"""\
        % test (1) Container Image Pages
        % me
        % {dt.fromtimestamp(atomic_reactor_start_time).strftime(format="%B %-d, %Y")}
        markdown file content
        """
    )

    def check_help_md(build_dir):
        help_md_path = build_dir.path / filename
        content = help_md_path.read_text()
        assert content == expect_content

    workflow.build_dir.for_each_platform(check_help_md)
