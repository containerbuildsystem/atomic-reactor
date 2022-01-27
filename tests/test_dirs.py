"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

import pytest
from atomic_reactor.constants import DOCKERFILE_FILENAME

from atomic_reactor.dirs import (
    BuildDir,
    BuildDirIsNotInitialized,
    ContextDir,
    DockerfileNotExist,
    FileCreationFunc,
    ImageInspectionData,
    RootBuildDir,
)
from atomic_reactor.source import DummySource
from dockerfile_parse import DockerfileParser


@pytest.fixture
def mock_source(source_dir):
    source = DummySource("git", "https://git.host/app-operator", workdir=str(source_dir))
    source.get()
    return source


def test_builddir_failure_on_nonexisting_path():
    with pytest.raises(FileNotFoundError, match="does not exist"):
        BuildDir(Path("some_dir"), "x86_64")


def test_builddir_dockerfile_path(tmpdir):
    dir_path = Path(tmpdir)
    dir_path.joinpath(DOCKERFILE_FILENAME).touch()
    build_dir = BuildDir(dir_path, "x86_64")
    assert Path(tmpdir.join(DOCKERFILE_FILENAME)) == build_dir.dockerfile_path


def test_builddir_dockerfile_path_returns_absolute_path(tmpdir):
    dir_path = Path(tmpdir)
    dir_path.joinpath(DOCKERFILE_FILENAME).touch()
    build_dir = BuildDir(dir_path, "x86_64")
    assert build_dir.dockerfile_path.is_absolute()


def test_builddir_dockerfile_not_support_linked_dockerfile(tmpdir):
    some_dir = Path(tmpdir)
    dockerfile = some_dir / DOCKERFILE_FILENAME
    dockerfile.touch()
    # link the Dockerfile
    build_dir = Path(tmpdir, "build_x86_64")
    build_dir.mkdir()
    build_dir.joinpath(DOCKERFILE_FILENAME).symlink_to(dockerfile)
    with pytest.raises(DockerfileNotExist, match="Dockerfile is linked from"):
        print(BuildDir(build_dir, "x86_64").dockerfile_path)


def test_builddir_get_parsed_dockerfile(tmpdir):
    dir_path = Path(tmpdir)
    dir_path.joinpath(DOCKERFILE_FILENAME).write_text("FROM fedora:35", "utf-8")
    build_dir = BuildDir(dir_path, "x86_64")
    assert isinstance(build_dir.dockerfile, DockerfileParser)


@pytest.mark.parametrize("inspection_data,expected_envs", [
    [{}, {"HOME": ""}],
    [{"Config": {}}, {"HOME": ""}],
    [{"Config": {"Env": {}}}, {"HOME": ""}],
    [{"Config": {"Env": []}}, {"HOME": ""}],
    [
        {"Config": {"Env": ["HOME=/home", "var2=--option=first"]}},
        {"HOME": "/home"},
    ],
    [
        {"Config": {"Env": {"HOME": "/home", "var2": "--option=first"}}},
        {"HOME": "/home"},
    ],
])
def test_builddir_get_parsed_dockerfile_with_parent_env(
    inspection_data: ImageInspectionData, expected_envs, tmpdir
):
    dir_path = Path(tmpdir)
    dir_path.joinpath(DOCKERFILE_FILENAME).write_text(
        "FROM base-image\nENV HOME=$HOME", "utf-8"
    )
    build_dir = BuildDir(dir_path, "x86_64")
    parsed_df = build_dir.dockerfile_with_parent_env(inspection_data)
    assert expected_envs == parsed_df.envs


def test_rootbuilddir_copy_sources(build_dir, mock_source):
    root_path = build_dir / "root_builddir"
    root_path.mkdir()

    platforms = ["x86_64", "ppc64le"]
    root = RootBuildDir(root_path)
    root.platforms = platforms
    root._copy_sources(mock_source)

    dockerfile = os.path.join(mock_source.path, DOCKERFILE_FILENAME)
    with open(dockerfile, "r") as f:
        original_content = f.read()

    for platform in platforms:
        copied_dockerfile = root.path / platform / DOCKERFILE_FILENAME
        assert copied_dockerfile.exists()
        assert copied_dockerfile.read_text("utf-8") == original_content


def test_rootbuilddir_has_sources_if_build_dirs_not_inited(build_dir):
    assert not RootBuildDir(build_dir).has_sources


def test_rootbuilddir_has_sources_no_builddir_created(build_dir):
    root = RootBuildDir(build_dir)
    root.platforms = ["x86_64"]
    assert not root.has_sources


def test_rootbuilddir_has_sources_partial_build_dirs(build_dir):
    build_dir.joinpath("x86_64").mkdir()
    root = RootBuildDir(build_dir)
    root.platforms = ["x86_64", "s390x"]
    assert not root.has_sources


def test_rootbuilddir_has_sources(build_dir):
    build_dir.joinpath("x86_64").mkdir()
    build_dir.joinpath("s390x").mkdir()
    root = RootBuildDir(build_dir)
    root.platforms = ["x86_64", "s390x"]
    assert root.has_sources


def test_rootbuilddir_get_any_platform(build_dir, mock_source):
    root = RootBuildDir(build_dir)
    root.init_build_dirs(["x86_64", "s390x"], mock_source)
    build_dir_1 = root.any_platform
    build_dir_2 = root.any_platform
    assert build_dir_1.path == build_dir_2.path
    assert build_dir_1.platform == build_dir_2.platform


def test_rootbuilddir_get_any_platform_fails_if_build_dirs_not_inited(build_dir):
    with pytest.raises(BuildDirIsNotInitialized, match="not initialized yet"):
        print(RootBuildDir(build_dir).any_platform)


def test_rootbuilddir_get_any_platform_by_different_platforms_order(build_dir, mock_source):
    root = RootBuildDir(build_dir)
    root.init_build_dirs(["x86_64", "s390x"], mock_source)
    build_dir_1 = root.any_platform

    root = RootBuildDir(build_dir)
    root.init_build_dirs(["s390x", "x86_64"], mock_source)
    build_dir_2 = root.any_platform

    assert build_dir_1.path == build_dir_2.path
    assert build_dir_1.platform == build_dir_2.platform


def handle_platform(build_dir: BuildDir) -> Any:
    if build_dir.platform == "x86_64":
        return "handled x86_64"
    elif build_dir.platform == "s390x":
        return {"reserved_build_id": 1000}
    return "the test does not care about this value"


def test_rootbuilddir_for_each_platform(build_dir, mock_source):
    root = RootBuildDir(build_dir)
    root.init_build_dirs(["x86_64", "s390x"], mock_source)
    results = root.for_each_platform(handle_platform)
    expected = {
        "x86_64": "handled x86_64",
        "s390x": {"reserved_build_id": 1000},
    }
    assert expected == results


def test_rootbuilddir_for_each_fails_if_build_dirs_not_inited(build_dir, mock_source):
    with pytest.raises(BuildDirIsNotInitialized, match="not initialized yet"):
        RootBuildDir(build_dir).for_each_platform(lambda path: None)


def failure_action(build_dir: BuildDir) -> Any:
    if build_dir.platform == "x86_64":
        raise ValueError("Error is raised when handling ...")
    return "the test does not care about this value"


def test_rootbuilddir_for_each_platform_failure_from_action(build_dir, mock_source):
    root = RootBuildDir(build_dir)
    root.init_build_dirs(["x86_64", "s390x"], mock_source)
    with pytest.raises(ValueError, match="Error is raised"):
        root.for_each_platform(failure_action)


def create_dockerfile(build_dir: BuildDir) -> Iterable[Path]:
    # Create: ./Dockerfile
    dockerfile = build_dir.path / DOCKERFILE_FILENAME
    dockerfile.write_text("FROM fedora:34", "utf-8")
    # Create: ./data/Dockerfile
    data_dir = build_dir.path / "data"
    data_dir.mkdir()
    data_json = data_dir / "data.json"
    data_json.write_text("{}", "utf-8")
    # Create: unpacked cachito archive
    # ./cachito-1
    # ./cachito-1/app
    # ./cachito-1/app/main.py
    unpacked = build_dir.path / "cachito-1"
    unpacked.mkdir()
    app_dir = unpacked / "app"
    app_dir.mkdir()
    app_dir.joinpath("main.py").write_text("print('Hello OSBS')", "utf-8")
    return [dockerfile, data_json,  unpacked]


def test_rootbuilddir_for_all_platforms_copy(build_dir, mock_source):
    root = RootBuildDir(build_dir)
    root.init_build_dirs(["x86_64", "s390x"], mock_source)
    results = root.for_all_platforms_copy(create_dockerfile)

    build_dir_s390x = build_dir.joinpath("s390x")
    expected_created_files = sorted([
        build_dir_s390x / DOCKERFILE_FILENAME,
        build_dir_s390x / "data" / "data.json",
        build_dir_s390x / "cachito-1",
    ])

    assert expected_created_files == sorted(results)

    expected_all_copied_files = expected_created_files + [
        build_dir_s390x / "cachito-1" / "app",
        build_dir_s390x / "cachito-1" / "app" / "main.py",
    ]

    for f in expected_all_copied_files:
        assert f.is_absolute()
        assert f.exists()


def create_file_outside_build_dir(build_dir: BuildDir) -> Iterable[Path]:
    fd, filename = tempfile.mkstemp()
    os.close(fd)
    return [Path(filename)]


def create_file_return_nonexisting_relative_path(build_dir: BuildDir) -> Iterable[Path]:
    return [Path("cachito-1/app/")]


def create_file_linked_to_outside_file(build_dir: BuildDir) -> Iterable[Path]:
    fd, filename = tempfile.mkstemp()
    os.close(fd)
    trust_file = build_dir.path.joinpath("please-do-trust-me.txt")
    trust_file.symlink_to(filename)
    return [trust_file]


def create_file_build_dir_is_returned_as_created(build_dir: BuildDir) -> Iterable[Path]:
    return [build_dir.path]


@pytest.mark.parametrize("creation_func,expected_err", [
    [
        create_file_outside_build_dir,
        pytest.raises(ValueError, match="File must be created inside"),
    ],
    [
        create_file_return_nonexisting_relative_path,
        pytest.raises(FileNotFoundError, match="does not exist inside build directory"),
    ],
    [
        create_file_linked_to_outside_file,
        pytest.raises(ValueError, match="File must be created inside"),
    ],
    [
        create_file_build_dir_is_returned_as_created,
        pytest.raises(ValueError, match="should not be added as a created"),
    ],
])
def test_rootbuilddir_for_all_platforms_copy_invalid_file_path(
    creation_func: FileCreationFunc, expected_err, build_dir, mock_source
):
    root = RootBuildDir(build_dir)
    root.init_build_dirs(["x86_64"], mock_source)
    with expected_err:
        root.for_all_platforms_copy(creation_func)


def test_rootbuilddir_for_all_platforms_copy_fails_if_build_dirs_not_inited(build_dir):
    with pytest.raises(BuildDirIsNotInitialized, match="not initialized yet"):
        RootBuildDir(build_dir).for_all_platforms_copy(lambda path: [])


def test_rootbuilddir_for_all_platforms_copy_preserve_permissions(build_dir, mock_source):
    root = RootBuildDir(build_dir)
    root.init_build_dirs(["aarch64", "x86_64"], mock_source)

    def create_files(d: BuildDir):
        f1 = d.path / "400.txt"
        f1.write_text("Preserve permissions for a single file")
        f1.chmod(0o400)

        subdir = d.path / "some-dir"
        subdir.mkdir()

        f2 = subdir / "666.txt"
        f2.write_text("Preserve permissions for files in a directory")
        f2.chmod(0o666)

        return [f1, subdir]

    root.for_all_platforms_copy(create_files)

    def check_rwx_perms(filepath: Path, expected_perms: int):
        # Check the 3 rwx bytes (owner, group and others) of file permissions
        actual_perms = filepath.stat().st_mode & 0o777
        assert actual_perms == expected_perms, (
            f"{filepath.relative_to(root.path)}: expected permissions {oct(expected_perms)}, "
            f"actual {oct(actual_perms)}"
        )

    for platform in ["aarch64", "x86_64"]:
        check_rwx_perms(root.path / platform / "400.txt", 0o400)
        check_rwx_perms(root.path / platform / "some-dir" / "666.txt", 0o666)


class TestContextDir:
    """Test ContextDir class implementation"""

    @pytest.mark.parametrize("parent_exists", [True, False])
    def test_ensure_dir_exists(self, parent_exists, tmpdir):
        parent_path = tmpdir.join("mounted_volume")
        if parent_exists:
            parent_path.mkdir()
        context_dir = ContextDir(Path(parent_path.join("context_dir")))
        assert context_dir._path.exists()

    def test_get_workflow_json(self, tmpdir):
        expected = Path(tmpdir.join("workflow.json"))
        assert expected == ContextDir(Path(tmpdir)).workflow_json
        # The ContextDir does not ensure workflow.json is created by itself.
        assert not expected.exists()

    @pytest.mark.parametrize("platform,error", [
        [None, pytest.raises(ValueError, match="No platform is specified")],
        ["", pytest.raises(ValueError, match="No platform is specified")],
        ["x86_64", None],
        ["some_arch", None],
    ])
    def test_get_platform_dir(self, platform, error, tmpdir):
        if error is None:
            dir_path = Path(tmpdir.join(platform))
            assert dir_path == ContextDir(Path(tmpdir)).get_platform_dir(platform)
            assert dir_path.exists()
        else:
            with error:
                ContextDir(Path(tmpdir)).get_platform_dir(platform)

    @pytest.mark.parametrize("platform,error", [
        [None, pytest.raises(ValueError, match="No platform is specified")],
        ["", pytest.raises(ValueError, match="No platform is specified")],
        ["x86_64", None],
        ["some_arch", None],
    ])
    def test_get_build_result_file(self, platform, error, tmpdir):
        if error is None:
            file_path = Path(tmpdir.join(platform, "build_result.json"))
            assert file_path == ContextDir(Path(tmpdir)).get_build_result_file(platform)
        else:
            with error:
                ContextDir(Path(tmpdir)).get_build_result_file(platform)
