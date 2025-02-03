"""
Copyright (c) 2024 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from pathlib import Path
from textwrap import dedent
import os.path
import json
from typing import Dict

import pytest
import yaml
from flexmock import flexmock

from atomic_reactor.constants import (
    CACHI2_BUILD_DIR,
    CACHI2_SINGLE_REMOTE_SOURCE_NAME,
    CACHI2_BUILD_APP_DIR,
    CACHI2_FOR_OUTPUT_DIR_OPT_FILE,
    CACHI2_INCLUDE_GIT_DIR_FILE,
    CACHI2_PKG_OPTIONS_FILE,
    CACHI2_ENV_JSON,
    CACHI2_SBOM_JSON,
)

from atomic_reactor.inner import DockerBuildWorkflow

from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.cachi2_init import (
    Cachi2InitPlugin,
)
from atomic_reactor.source import SourceConfig
from tests.mock_env import MockEnv

from tests.stubs import StubSource
from tests.utils.test_cachi2 import Symlink, write_file_tree


REMOTE_SOURCE_REPO = 'https://git.example.com/team/repo.git'
REMOTE_SOURCE_REF = 'b55c00f45ec3dfee0c766cea3d395d6e21cc2e5a'
SECOND_REMOTE_SOURCE_REPO = 'https://git.example.com/other-team/other-repo.git'
SECOND_REMOTE_SOURCE_REF = 'd55c00f45ec3dfee0c766cea3d395d6e21cc2e5c'

MOCKED_CLONE_FILE = "clone.txt"


def mock_reactor_config(workflow, data=None):
    config = yaml.safe_load(data)
    workflow.conf.conf = config


def mock_repo_config(workflow, data=None):
    if data is None:
        data = dedent("""\
            remote_source:
                repo: {}
                ref: {}
            """.format(REMOTE_SOURCE_REPO, REMOTE_SOURCE_REF))

    workflow._tmpdir.joinpath('container.yaml').write_text(data, "utf-8")

    # The repo config is read when SourceConfig is initialized. Force
    # reloading here to make usage easier.
    workflow.source.config = SourceConfig(str(workflow._tmpdir))


@pytest.fixture
def mocked_cachi2_init():
    def clone_f(repo, target_dir, ref):
        with open(target_dir / "clone.txt", "w") as f:
            f.write(f"{repo}:{ref}")
            f.flush()

    mocked = flexmock(Cachi2InitPlugin)
    mocked.should_receive('clone_remote_source').replace_with(clone_f)
    return Cachi2InitPlugin


@pytest.fixture
def workflow(workflow: DockerBuildWorkflow, source_dir):
    # Stash the tmpdir in workflow so it can be used later
    workflow._tmpdir = source_dir

    class MockSource(StubSource):

        def __init__(self, workdir):
            super(MockSource, self).__init__()
            self.workdir = workdir
            self.path = workdir

    workflow.source = MockSource(str(source_dir))

    mock_repo_config(workflow)

    workflow.build_dir.init_build_dirs(["x86_64", "ppc64le"], workflow.source)

    return workflow


def assert_cachi2_init_files(
        workflow,
        remote_source_name: str,
        expected_pkg_opt: Dict,
        expected_for_output: str):

    cachi2_build_path = workflow.build_dir.path / CACHI2_BUILD_DIR
    assert os.path.exists(cachi2_build_path)

    remote_source_path = cachi2_build_path / remote_source_name
    assert os.path.exists(remote_source_path)

    clone_path = remote_source_path / CACHI2_BUILD_APP_DIR / MOCKED_CLONE_FILE
    assert os.path.exists(clone_path)

    cachi2_pkg_opt_path = remote_source_path / CACHI2_PKG_OPTIONS_FILE
    assert os.path.exists(cachi2_pkg_opt_path)
    with open(cachi2_pkg_opt_path, "r") as f:
        assert json.load(f) == expected_pkg_opt

    cachi2_for_output_opt_path = remote_source_path / CACHI2_FOR_OUTPUT_DIR_OPT_FILE
    assert os.path.exists(cachi2_for_output_opt_path)
    with open(cachi2_for_output_opt_path, "r") as f:
        assert f.read() == expected_for_output


def test_single_remote_source_initialization(workflow, mocked_cachi2_init):
    """Tests initialization or repos for single remote source"""
    result = mocked_cachi2_init(workflow).run()

    assert_cachi2_init_files(
        workflow,
        CACHI2_SINGLE_REMOTE_SOURCE_NAME,
        {"packages": [{"path": ".", "type": "gomod"}], "flags": []},
        '/remote-source')

    assert result == [{
        "name": None,
        "source_path": str(
            workflow.build_dir.path / CACHI2_BUILD_DIR / CACHI2_SINGLE_REMOTE_SOURCE_NAME),
        "remote_source": {
            "repo": REMOTE_SOURCE_REPO,
            "ref": REMOTE_SOURCE_REF,
            "pkg_managers": ["gomod"],
        }
    }]


def test_empty_list_pkg_managers(workflow, mocked_cachi2_init):
    """Test if when empty_list of pkg managers is specified,
    sbom and env-var are generated and cachi2 config skipped"""
    first_remote_source_name = "first"

    remote_source_config = dedent(
        f"""\
        remote_sources:
        - name: {first_remote_source_name}
          remote_source:
            repo: {REMOTE_SOURCE_REPO}
            ref: {REMOTE_SOURCE_REF}
            pkg_managers: []
        """
    )

    mock_repo_config(workflow, remote_source_config)

    reactor_config = dedent("""\
        allow_multiple_remote_sources: True
        """)
    mock_reactor_config(workflow, reactor_config)

    result = mocked_cachi2_init(workflow).run()
    assert result == [{
        "name": first_remote_source_name,
        "source_path": str(workflow.build_dir.path / CACHI2_BUILD_DIR / first_remote_source_name),
        "remote_source": {
            "repo": REMOTE_SOURCE_REPO,
            "ref": REMOTE_SOURCE_REF,
            "pkg_managers": [],
        }
    }]

    source_path = workflow.build_dir.path / CACHI2_BUILD_DIR / first_remote_source_name

    # test if bom and env have been created
    assert (source_path / CACHI2_SBOM_JSON).is_file()
    assert (source_path / CACHI2_ENV_JSON).is_file()

    # test if clone happened
    assert (source_path / CACHI2_BUILD_APP_DIR / MOCKED_CLONE_FILE).is_file()

    # test that cachi2 pkg config file hasn't been generated
    assert not (source_path / CACHI2_PKG_OPTIONS_FILE).is_file()


def test_include_git_dir_flag(workflow, mocked_cachi2_init):
    """Test if git directory flag is processed correctly by creating a flag file"""
    first_remote_source_name = "first"

    remote_source_config = dedent(
        f"""\
        remote_sources:
        - name: {first_remote_source_name}
          remote_source:
            repo: {REMOTE_SOURCE_REPO}
            ref: {REMOTE_SOURCE_REF}
            pkg_managers: []
            flags:
              - include-git-dir
        """
    )

    mock_repo_config(workflow, remote_source_config)

    reactor_config = dedent("""\
        allow_multiple_remote_sources: True
        """)
    mock_reactor_config(workflow, reactor_config)

    result = mocked_cachi2_init(workflow).run()
    assert result == [{
        "name": first_remote_source_name,
        "source_path": str(workflow.build_dir.path / CACHI2_BUILD_DIR / first_remote_source_name),
        "remote_source": {
            "repo": REMOTE_SOURCE_REPO,
            "ref": REMOTE_SOURCE_REF,
            "pkg_managers": [],
            "flags": ["include-git-dir"]
        }
    }]

    source_path = workflow.build_dir.path / CACHI2_BUILD_DIR / first_remote_source_name

    # test if include-git-dir flag file is created
    assert (source_path / CACHI2_INCLUDE_GIT_DIR_FILE).is_file()


def test_multi_remote_source_initialization(workflow, mocked_cachi2_init):
    """Tests initialization or repos for multiple remote sources"""

    first_remote_source_name = "first"
    second_remote_source_name = "second"

    remote_source_config = dedent(
        f"""\
        remote_sources:
        - name: {first_remote_source_name}
          remote_source:
            repo: {REMOTE_SOURCE_REPO}
            ref: {REMOTE_SOURCE_REF}
            flags:
            - gomod-vendor
            pkg_managers:
            - gomod
        - name: {second_remote_source_name}
          remote_source:
            repo: {SECOND_REMOTE_SOURCE_REPO}
            ref: {SECOND_REMOTE_SOURCE_REF}
        """
    )

    mock_repo_config(workflow, remote_source_config)

    reactor_config = dedent("""\
        allow_multiple_remote_sources: True
        """)
    mock_reactor_config(workflow, reactor_config)

    result = mocked_cachi2_init(workflow).run()

    assert_cachi2_init_files(
        workflow,
        first_remote_source_name,
        {
            "packages": [{"path": ".", "type": "gomod"}],
            "flags": ["gomod-vendor"]
        },
        f'/remote-source/{first_remote_source_name}')
    assert_cachi2_init_files(
        workflow,
        second_remote_source_name,
        {"packages": [{"path": ".", "type": "gomod"}], "flags": []},
        f'/remote-source/{second_remote_source_name}')

    assert result == [{
        "name": first_remote_source_name,
        "source_path": str(workflow.build_dir.path / CACHI2_BUILD_DIR / first_remote_source_name),
        "remote_source": {
            "repo": REMOTE_SOURCE_REPO,
            "ref": REMOTE_SOURCE_REF,
            "pkg_managers": ["gomod"],
            "flags": ["gomod-vendor"],
        }
    }, {
        "name": second_remote_source_name,
        "source_path": str(workflow.build_dir.path / CACHI2_BUILD_DIR / second_remote_source_name),
        "remote_source": {
            "repo": SECOND_REMOTE_SOURCE_REPO,
            "ref": SECOND_REMOTE_SOURCE_REF,
            "pkg_managers": ["gomod"],
        }
    }]


def test_no_fail_when_missing_cachito_config(workflow, mocked_cachi2_init):
    """Cachi2 is not dependent on cachito config"""
    reactor_config = dedent("""\
        version: 1
        """)
    mock_reactor_config(workflow, reactor_config)

    result = mocked_cachi2_init(workflow).run()
    assert result


def test_ignore_when_missing_remote_source_config(workflow, mocked_cachi2_init):
    """Plugin should just skip when remote source is not configured"""
    remote_source_config = dedent("""---""")
    mock_repo_config(workflow, remote_source_config)
    result = mocked_cachi2_init(workflow).run()
    assert result is None


def test_disallowed_multiple_remote_sources(workflow):
    first_remote_source_name = 'gomod'

    container_yaml_config = dedent(
        f"""\
                remote_sources:
                - name: {first_remote_source_name}
                  remote_source:
                    repo: {REMOTE_SOURCE_REPO}
                    ref: {REMOTE_SOURCE_REF}
                """
    )

    reactor_config = dedent("""\
                version: 1
                allow_multiple_remote_sources: false
                """)
    mock_repo_config(workflow, data=container_yaml_config)
    mock_reactor_config(workflow, reactor_config)

    err_msg = (
        "Multiple remote sources are not enabled, "
        "use single remote source in container.yaml"
    )
    result = run_plugin_with_args(workflow, expect_result=False, expect_error=err_msg)
    assert result is None


def test_multiple_remote_sources_non_unique_names(workflow):
    container_yaml_config = dedent("""\
            remote_sources:
            - name: same
              remote_source:
                repo: https://git.example.com/team/repo.git
                ref: a55c00f45ec3dfee0c766cea3d395d6e21cc2e5a
            - name: same
              remote_source:
                repo: https://git.example.com/team/repo.git
                ref: a55c00f45ec3dfee0c766cea3d395d6e21cc2e5a
            - name: bit-different
              remote_source:
                repo: https://git.example.com/team/repo.git
                ref: a55c00f45ec3dfee0c766cea3d395d6e21cc2e5a
            """)
    mock_repo_config(workflow, data=container_yaml_config)

    reactor_config = dedent("""\
        allow_multiple_remote_sources: True
        """)
    mock_reactor_config(workflow, reactor_config)

    err_msg = (
        r"Provided remote sources parameters contain non unique names: \['same'\]"
    )
    result = run_plugin_with_args(workflow, expect_result=False, expect_error=err_msg)
    assert result is None


def test_path_out_of_repo(workflow, mocked_cachi2_init):
    """Should fail when path is outside of repository"""
    container_yaml_config = dedent("""\
            remote_sources:
            - name: bit-different
              remote_source:
                repo: https://git.example.com/team/repo.git
                ref: a55c00f45ec3dfee0c766cea3d395d6e21cc2e5a
                packages:
                  gomod:
                  - path: "/out/of/repo"
            """)
    mock_repo_config(workflow, data=container_yaml_config)

    reactor_config = dedent("""\
        allow_multiple_remote_sources: True
        """)
    mock_reactor_config(workflow, reactor_config)

    err_msg = (
        "gomod:path: path '/out/of/repo' must be relative within remote source repository"
    )
    with pytest.raises(ValueError) as exc_info:
        mocked_cachi2_init(workflow).run()

    assert err_msg in str(exc_info)


def test_dependency_replacements(workflow):
    run_plugin_with_args(workflow, dependency_replacements={"dep": "something"},
                         expect_error="Dependency replacements are not supported by Cachi2")


def test_enforce_sandbox(workflow: DockerBuildWorkflow) -> None:
    """Should remove symlink pointing outside of repository"""
    container_yaml_config = dedent(f"""\
            remote_source:
              flags:
                - remove-unsafe-symlinks
              repo: {REMOTE_SOURCE_REPO}
              ref: {REMOTE_SOURCE_REF}
              pkg_managers: []
            """)

    mock_repo_config(workflow, data=container_yaml_config)

    def clone_f(repo, target_dir, ref):
        bad_symlink = Path(target_dir / 'symlink_to_root')
        with open(target_dir / "clone.txt", "w") as f:
            f.write(f"{repo}:{ref}")
            f.flush()
        write_file_tree({bad_symlink: Symlink("/")}, target_dir)
        assert Path(target_dir / bad_symlink).exists()

    mocked = flexmock(Cachi2InitPlugin)
    mocked.should_receive('clone_remote_source').replace_with(clone_f)
    Cachi2InitPlugin(workflow).run()

    assert not Path(workflow.build_dir.path / CACHI2_BUILD_DIR / "remote-source"
                    / CACHI2_BUILD_APP_DIR / "symlink_to_root").exists()


def run_plugin_with_args(workflow, dependency_replacements=None, expect_error=None,
                         expect_result=True, expected_plugin_results=None):
    runner = (MockEnv(workflow)
              .for_plugin(Cachi2InitPlugin.key)
              .set_plugin_args({"dependency_replacements": dependency_replacements})
              .create_runner())

    if expect_error:
        with pytest.raises(PluginFailedException, match=expect_error):
            runner.run()
        return

    results = runner.run()[Cachi2InitPlugin.key]

    if expect_result:
        assert results == expected_plugin_results

    return results
