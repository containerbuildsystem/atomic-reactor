"""
Copyright (c) 2024 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import os
from pathlib import Path
from typing import Union

from flexmock import flexmock

from atomic_reactor.utils.cachi2 import (
    SymlinkSandboxError,
    convert_SBOM_to_ICM,
    enforce_sandbox,
    remote_source_to_cachi2,
    gen_dependency_from_sbom_component,
    generate_request_json,
    clone_only,
    validate_paths,
    has_git_submodule_manager,
    get_submodules_request_json_deps,
    get_submodules_sbom_components,
)

import pytest

from unittest import mock


def mock_repo_submodules(name, url, hexsha):
    submodule = flexmock(name=name, url=url, hexsha=hexsha)
    return flexmock(submodules=[submodule])


@pytest.fixture
def mocked_repo_submodules():
    """Mock submodules repo"""
    return mock_repo_submodules(
        "example-repo",
        "https://example.com/repo.git",
        "a816faea7b2a62f26dd0ae763fb657149ca36a1c"
    )


@pytest.mark.parametrize(('input_remote_source', 'expected_cachi2'), [
    pytest.param(
        {"pkg_managers": ["gomod"]},
        {"flags": [], "packages": [{"path": ".", "type": "gomod"}]},
        id="pkg_manager_plain",
    ),
    pytest.param(
        {"pkg_managers": ["rubygems"]},
        {"flags": [], "packages": [{"path": ".", "type": "bundler"}]},
        id="pkg_rubygems_to_bundler",
    ),
    pytest.param(
        {},
        {"flags": [], "packages": [{"path": ".", "type": "gomod"}]},
        id="pkg_manager_missing",
    ),
    pytest.param(
        {"pkg_managers": ["gomod"], "packages": {"gomod": [{"path": "operator"}]}},
        {"flags": [], "packages": [{"path": "operator", "type": "gomod"}]},
        id="pkg_manager_single_path"
    ),
    pytest.param(
        {"pkg_managers": ["gomod"], "packages": {"gomod": [
            {"path": "."}, {"path": "operator"}
        ]}},
        {"flags": [], "packages": [
            {"path": ".", "type": "gomod"}, {"path": "operator", "type": "gomod"}
        ]},
        id="pkg_manager_multiple_paths"
    ),
    pytest.param(
        {"pkg_managers": ["pip"], "packages": {
            "pip": [{
                "path": "src/web",
                "requirements_files": ["requirements.txt", "requirements-extras.txt"],
                "requirements_build_files": [
                    "requirements-build.txt", "requirements-build-extras.txt"
                ]
            }, {
                "path": "src/workers"
            }]
        }},
        {"flags": [], "packages": [
            {
                "path": "src/web", "type": "pip",
                "requirements_files": ["requirements.txt", "requirements-extras.txt"],
                "requirements_build_files": [
                    "requirements-build.txt", "requirements-build-extras.txt"
                    ]
            },
            {"path": "src/workers", "type": "pip"}]},
        id="pip_extra_options"
    ),
    pytest.param(
        {"pkg_managers": ["gomod", "npm"], "packages": {"gomod": [{"path": "operator"}]}},
        {"flags": [], "packages": [
            {"path": "operator", "type": "gomod"}, {"path": ".", "type": "npm"}
        ]},
        id="mixed_pkg_managers"
    ),
    pytest.param(
        {"pkg_managers": ["gomod"], "flags": ["gomod-vendor"]},
        {"flags": ["gomod-vendor"], "packages": [{"path": ".", "type": "gomod"}]},
        id="pkg_manager_with_flags",
    ),
    pytest.param(
        {"pkg_managers": ["gomod"], "flags": [
            "remove-unsafe-symlinks", "gomod-vendor", "include-git-dir"
        ]},
        {"flags": ["gomod-vendor"], "packages": [{"path": ".", "type": "gomod"}]},
        id="unsupported_flags",
    ),
    pytest.param(
        {"pkg_managers": ["git-submodule"]},
        {"flags": [], "packages": []},
        id="unsupported_git_submodule",
    ),
    pytest.param(
        {"pkg_managers": []},
        {"flags": [], "packages": []},
        id="empty_package_managers",
    ),
])
def test_remote_source_to_cachi2_conversion(input_remote_source, expected_cachi2):
    """Test conversion of remote_source (cachito) configuration from container yaml
    into cachi2 params"""
    assert remote_source_to_cachi2(input_remote_source) == expected_cachi2


@pytest.mark.parametrize("remote_source_packages,expected_err", [
    pytest.param(
        {"gomod": [{"path": "/path/to/secret"}]},
        "gomod:path: path '/path/to/secret' must be relative within remote source repository",
        id="absolute_path"
    ),
    pytest.param(
        {"gomod": [{"unknown": "/path/to/secret"}]},
        "unexpected key 'unknown' in 'gomod' config",
        id="unknown_option"
    ),
    pytest.param(
        {"gomod": [{"path": "path/../../../to/secret"}]},
        (
            "gomod:path: path 'path/../../../to/secret' must be relative "
            "within remote source repository"
        ),
        id="path_traversal"
    ),
    pytest.param(
        {
            "pip": [{
                "path": "/src/web",
            }]
        },
        "pip:path: path '/src/web' must be relative within remote source repository",
        id="pip_absolute_path"
    ),
    pytest.param(
        {
            "pip": [{
                "requirements_files": ["requirements.txt", "/requirements-extras.txt"],
            }]
        },
        (
            "pip:requirements_files: path '/requirements-extras.txt' "
            "must be relative within remote source repository"
        ),
        id="pip_requirements_files_absolute_path"
    ),
    pytest.param(
        {
            "pip": [{
                "requirements_build_files": [
                    "requirements-build.txt", "/requirements-build-extras.txt"
                ]
            }]
        },
        (
            "pip:requirements_build_files: path '/requirements-build-extras.txt' "
            "must be relative within remote source repository"
        ),
        id="pip_requirements_build_files_absolute_path"
    )
])
def test_remote_source_invalid_paths(tmpdir, remote_source_packages, expected_err):
    """Test conversion of remote_source (cachito) configuration from container yaml
    into cachi2 params"""
    with pytest.raises(ValueError) as exc_info:
        validate_paths(Path(tmpdir), remote_source_packages)

    assert str(exc_info.value) == expected_err


def test_remote_source_path_to_symlink_out_of_repo(tmpdir):
    """If cloned repo contains symlink that points out of repo,
    path in packages shouldn't be allowed"""
    tmpdir_path = Path(tmpdir)

    symlink_target = tmpdir_path/"dir_outside"
    symlink_target.mkdir()

    cloned = tmpdir_path/'app'
    cloned.mkdir()

    symlink = cloned/"symlink"
    symlink.symlink_to(symlink_target, target_is_directory=True)

    remote_source_packages = {
        "pip": [{
            "path": "symlink",
        }]
    }

    expected_err = "pip:path: path 'symlink' must be relative within remote source repository"

    with pytest.raises(ValueError) as exc_info:
        validate_paths(cloned, remote_source_packages)

    assert str(exc_info.value) == expected_err


@pytest.mark.parametrize(('sbom', 'expected_icm'), [
    pytest.param(
        {
            "bomFormat": "CycloneDX",
            "components": [{
                "name": "unsafe",
                "purl": "pkg:golang/unsafe?type=package",
                "properties": [{
                    "name": "cachi2:found_by",
                    "value": "cachi2",
                }],
                "type": "library",
            }],
            "metadata": {
                "tools": [{
                    "vendor": "red hat",
                    "name": "cachi2"
                }]
            },
            "specVersion": "1.4",
            "version": 1
        },
        {
            "image_contents": [
                {"purl": "pkg:golang/unsafe?type=package"},
            ],
            "metadata": {
                "icm_spec": (
                    "https://raw.githubusercontent.com/containerbuildsystem/atomic-reactor/"
                    "f4abcfdaf8247a6b074f94fa84f3846f82d781c6/atomic_reactor/"
                    "schemas/content_manifest.json"
                ),
                "icm_version": 1,
                "image_layer_index": -1
            }
        },
        id="easy",
    ),
])
def test_convert_SBOM_to_ICM(sbom, expected_icm):
    """Test conversion from cachi2 SBOM into ICM format"""
    assert convert_SBOM_to_ICM(sbom) == expected_icm


@pytest.mark.parametrize(("sbom_comp", "expected"), [
    pytest.param(
        {
            "name": "github.com/cachito-testing/testmodule",
            "version": "v1.0.0",
            "purl": "pkg:golang/github.com/cachito-testing/testmodule@v1.0.0?type=module",
            "type": "library"
        },
        {
            "name": "github.com/cachito-testing/testmodule",
            "replaces": None,
            "type": "gomod",
            "version": "v1.0.0"
        },
        id="type_gomod"
    ),
    pytest.param(
        {
            "name": "github.com/cachito-testing",
            "version": "v1.0.0",
            "purl": "pkg:golang/github.com/cachito-testing@v1.0.0",
            "type": "library"
        },
        {
            "name": "github.com/cachito-testing",
            "replaces": None,
            "type": "go-package",
            "version": "v1.0.0"
        },
        id="type_go-package"
    ),
    pytest.param(
        {
            "name": "cachito-npm-without-deps",
            "purl": ("pkg:npm/cachito-npm-without-deps?"
                     "vcs_url=git%2Bhttps://github.com/cachito-testing/"
                     "cachito-npm-without-deps.git%402f0ce1d7b1f8b35572d919428b965285a69583f6"),
            "type": "library"
        },
        {
            "name": "cachito-npm-without-deps",
            "replaces": None,
            "type": "npm",
            "version": (
                "git+https://github.com/cachito-testing/cachito-npm-without-deps.git@"
                "2f0ce1d7b1f8b35572d919428b965285a69583f6"),
        },
        id="type_npm"
    ),
    pytest.param(
        {
            "name": "aiowsgi",
            "version": "0.8",
            "purl": "pkg:pypi/aiowsgi@0.8",
            "type": "library"
        },
        {
            "name": "aiowsgi",
            "replaces": None,
            "type": "pip",
            "version": "0.8"
        },
        id="type_pip"
    ),
    pytest.param(
        {
            "name": "validate_url",
            "version": "1.0.5",
            "purl": "pkg:gem/validate_url@1.0.5",
            "type": "library"
        },
        {
            "name": "validate_url",
            "replaces": None,
            "type": "rubygems",
            "version": "1.0.5"
        },
        id="type_rubygem"
    ),
    pytest.param(
        {
            "name": "cachito-testing",
            "version": "1.0.0-5",
            "purl": "pkg:rpm/cachito-testing@1.0.0-5",
            "type": "library"
        },
        {
            "name": "cachito-testing",
            "replaces": None,
            "type": "rpm",
            "version": "1.0.0-5"
        },
        id="type_rpm"
    ),
    pytest.param(
        {
            "name": "github.com/cachito-testing",
            "version": "v1.0.0",
            "purl": "pkg:somethingnew/github.com/cachito-testing@v1.0.0",
            "type": "library"
        },
        {
            "name": "github.com/cachito-testing",
            "replaces": None,
            "type": "somethingnew",
            "version": "v1.0.0"
        },
        id="type_somethingnew"
    ),
    pytest.param(
        {
            "name": "github.com/cachito-testing",
            "version": "v1.0.0",
            "purl": "pkg:golang/github.com/cachito-testing#path",
            "type": "library"
        },
        {
            "name": "github.com/cachito-testing",
            "replaces": None,
            "type": "go-package",
            "version": "./path"
        },
        id="version_golang_path"
    ),
    pytest.param(
        {
            # vcs_url has priority
            "name": "cachito-npm-without-deps",
            "purl": ("pkg:npm/cachito-npm-without-deps?"
                     "vcs_url=git%2Bhttps://github.com/cachito-testing/"
                     "cachito-npm-without-deps.git%402f0ce1d7b1f8b35572d919428b965285a69583f6&"
                     "download_url=https://example.com/pkg#path"),
            "type": "library",
            "version": "1.2.3"
        },
        {
            "name": "cachito-npm-without-deps",
            "replaces": None,
            "type": "npm",
            "version": (
                "git+https://github.com/cachito-testing/cachito-npm-without-deps.git@"
                "2f0ce1d7b1f8b35572d919428b965285a69583f6#path"),
        },
        id="version_vsc_url"
    ),
    pytest.param(
        {
            # download_url has priority
            "name": "cachito-npm-without-deps",
            "purl": ("pkg:npm/cachito-npm-without-deps?"
                     "download_url=https://example.com/pkg#path"),
            "type": "library",
            "version": "1.2.3"
        },
        {
            "name": "cachito-npm-without-deps",
            "replaces": None,
            "type": "npm",
            "version": "https://example.com/pkg#path",
        },
        id="version_download_url"
    ),
    pytest.param(
        {
            # path has priority
            "name": "cachito-npm-without-deps",
            "purl": "pkg:npm/cachito-npm-without-deps#path",
            "type": "library",
            "version": "1.0.0"
        },
        {
            "name": "cachito-npm-without-deps",
            "replaces": None,
            "type": "npm",
            "version": "file:path",
        },
        id="version_path"
    ),
    pytest.param(
        {
            "name": "cachito-npm-without-deps",
            "purl": "pkg:npm/cachito-npm-without-deps",
            "type": "library",
            "version": "1.0.0"
        },
        {
            "name": "cachito-npm-without-deps",
            "replaces": None,
            "type": "npm",
            "version": "1.0.0",
        },
        id="version_version"
    ),
    pytest.param(
        {
            "name": "cachito-npm-without-deps",
            "purl": "pkg:npm/cachito-npm-without-deps",
            "type": "library",
        },
        {
            "name": "cachito-npm-without-deps",
            "replaces": None,
            "type": "npm",
            "version": None,
        },
        id="version_missing"
    ),
    pytest.param(
        {
            "name": "cachito-npm-without-deps",
            "purl": "pkg:npm/cachito-npm-without-deps",
            "type": "library",
            "properties": [
                {
                    "name": "cdx:npm:package:development",
                    "value": "true"
                }
            ],
        },
        {
            "name": "cachito-npm-without-deps",
            "replaces": None,
            "type": "npm",
            "version": None,
            "dev": True,
        },
        id="npm_dev"
    ),
    pytest.param(
        {
            "name": "flit-core",
            "purl": "pkg:pip/flit-core@3.10.1",
            "type": "library",
            "version": "3.10.1",
            "properties": [
                {
                    "name": "cdx:pip:package:build-dependency",
                    "value": "true"
                }
            ],
        },
        {
            "name": "flit-core",
            "replaces": None,
            "type": "pip",
            "version": "3.10.1",
            "dev": True,
        },
        id="pip_dev"
    ),
    pytest.param(
        {
            "name": "validate_url",
            "version": "1.0.5",
            "purl": "pkg:gem/validate_url#subpath",
            "type": "library"
        },
        {
            "name": "validate_url",
            "replaces": None,
            "type": "rubygems",
            "version": "./subpath"
        },
        id="type_rubygem_subpath_only"
    ),
])
def test_gen_dependency_from_sbom_component(sbom_comp, expected):
    """Test generating request.json dependency from sbom component"""
    assert gen_dependency_from_sbom_component(sbom_comp) == expected


def test_generate_request_json():
    """Test generating request.json from cachi2 SBOM and OSBS metadata"""
    remote_source = {
        "repo": "https://example.com/org/repo.git",
        "ref": "7d669deedc3fd0e9199213e1f66056bb9f388394",
        "pkg_managers": ["gomod"],
        "flags": ["gomod-vendor-check"]
    }

    remote_source_sbom = {
        "bomFormat": "CycloneDX",
        "components": [
            {
                "name": "bytes",
                "purl": "pkg:golang/bytes?type=package",
                "properties": [
                    {
                        "name": "cachi2:found_by",
                        "value": "cachi2"
                    }
                ],
                "type": "library"
            },
        ],
    }

    remote_source_env_json = [
        {
          "name": "GOCACHE",
          "value": "deps/gomod",
        },
    ]

    expected = {
        "dependencies": [
            {
                "name": "bytes",
                "replaces": None,
                "type": "go-package",
                "version": None,
            },
        ],
        "pkg_managers": ["gomod"],
        "repo": "https://example.com/org/repo.git",
        "ref": "7d669deedc3fd0e9199213e1f66056bb9f388394",
        "environment_variables": {"GOCACHE": "deps/gomod"},
        "flags": ["gomod-vendor-check"],
        "packages": [],
    }

    assert generate_request_json(
        remote_source, remote_source_sbom, remote_source_env_json
    ) == expected


@pytest.mark.parametrize('remote_source,expected', [
    pytest.param(
        {
            "pkg_managers": []
        },
        True,
        id="empty_list"
    ),
    pytest.param(
        {
            "pkg_managers": ["git-submodule"]
        },
        True,
        id="git_submodule"
    ),
    pytest.param(
        {
            "pkg_managers": ["gomod"]
        },
        False,
        id="gomod"
    ),
    pytest.param(
        {
            "pkg_managers": ["gomod", "git-submodule"]
        },
        False,
        id="gomod_and_git_submodule"
    ),
    pytest.param(
        {},
        False,
        id="undefined"
    ),
    pytest.param(
        {
            "pkg_managers": None
        },
        False,
        id="explicit_none"
    ),
])
def test_clone_only(remote_source, expected):
    """Test if clone_only is evaluate correctly only from empty list of pkg_managers"""
    assert clone_only(remote_source) == expected


@pytest.mark.parametrize('remote_source,expected', [
    pytest.param(
        {
            "pkg_managers": []
        },
        False,
        id="empty_list"
    ),
    pytest.param(
        {
            "pkg_managers": ["git-submodule"]
        },
        True,
        id="git_submodule"
    ),
    pytest.param(
        {
            "pkg_managers": ["gomod"]
        },
        False,
        id="gomod"
    ),
    pytest.param(
        {
            "pkg_managers": ["gomod", "git-submodule"]
        },
        True,
        id="gomod_and_git_submodule"
    ),
    pytest.param(
        {},
        False,
        id="undefined"
    ),
    pytest.param(
        {
            "pkg_managers": None
        },
        False,
        id="explicit_none"
    ),
])
def test_has_git_submodule_manager(remote_source, expected):
    """Test if has_git_submodule_manager correctly detects git-submodule"""
    assert has_git_submodule_manager(remote_source) == expected


class Symlink(str):
    """
    Use this to create symlinks via write_file_tree().

    The value of a Symlink instance is the target path (path to make a symlink to).
    """


def write_file_tree(tree_def: dict, rooted_at: Union[str, Path], *, exist_dirs_ok: bool = False):
    """
    Write a file tree to disk.

    :param tree_def: Definition of file tree, see usage for intuitive examples
    :param rooted_at: Root of file tree, must be an existing directory
    :param exist_dirs_ok: If True, existing directories will not cause this function to fail
    """
    root = Path(rooted_at)
    for entry, value in tree_def.items():
        entry_path = root / entry
        if isinstance(value, Symlink):
            os.symlink(value, entry_path)
        elif isinstance(value, str):
            entry_path.write_text(value)
        else:
            entry_path.mkdir(exist_ok=exist_dirs_ok)
            write_file_tree(value, entry_path)


@pytest.mark.parametrize(
    "file_tree,bad_symlink",
    [
        # good
        pytest.param({}, None, id="empty-no-symlink"),
        pytest.param({"symlink_to_self": Symlink(".")}, None, id="self-symlink-ok"),
        pytest.param(
            {"subdir": {"symlink_to_parent": Symlink("..")}}, None, id="parent-symlink-ok"
        ),
        pytest.param(
            {"symlink_to_subdir": Symlink("subdir/some_file"), "subdir": {"some_file": "foo"}},
            None,
            id="subdir-symlink-ok",
        ),
        # bad
        pytest.param(
            {"symlink_to_parent": Symlink("..")}, "symlink_to_parent", id="parent-symlink-bad"
        ),
        pytest.param({"symlink_to_root": Symlink("/")}, "symlink_to_root", id="root-symlink-bad"),
        pytest.param(
            {"subdir": {"symlink_to_parent_parent": Symlink("../..")}},
            "subdir/symlink_to_parent_parent",
            id="parent-parent-symlink-bad",
        ),
        pytest.param(
            {"subdir": {"symlink_to_root": Symlink("/")}},
            "subdir/symlink_to_root",
            id="subdir-root-symlink-bad",
        ),
    ],
)
def test_enforce_sandbox(file_tree, bad_symlink, tmp_path):
    write_file_tree(file_tree, tmp_path)
    if bad_symlink:
        error = f"The destination of {bad_symlink!r} is outside of cloned repository"
        with pytest.raises(SymlinkSandboxError, match=error):
            enforce_sandbox(tmp_path, remove_unsafe_symlinks=False)
        assert Path(tmp_path / bad_symlink).exists()
        enforce_sandbox(tmp_path, remove_unsafe_symlinks=True)
        assert not Path(tmp_path / bad_symlink).exists()
    else:
        enforce_sandbox(tmp_path, remove_unsafe_symlinks=False)
        enforce_sandbox(tmp_path, remove_unsafe_symlinks=True)


def test_enforce_sandbox_symlink_loop(tmp_path, caplog):
    file_tree = {"foo_b": Symlink("foo_a"), "foo_a": Symlink("foo_b")}
    write_file_tree(file_tree, tmp_path)
    enforce_sandbox(tmp_path, remove_unsafe_symlinks=True)
    assert "Symlink loop from " in caplog.text


@mock.patch("pathlib.Path.resolve")
def test_enforce_sandbox_runtime_error(mock_resolve, tmp_path):
    error = "RuntimeError is triggered"

    def side_effect():
        raise RuntimeError(error)

    mock_resolve.side_effect = side_effect

    file_tree = {"foo_b": Symlink("foo_a"), "foo_a": Symlink("foo_b")}
    write_file_tree(file_tree, tmp_path)
    with pytest.raises(RuntimeError, match=error):
        enforce_sandbox(tmp_path, remove_unsafe_symlinks=True)


def test_get_submodules_sbom_components(mocked_repo_submodules):
    assert get_submodules_sbom_components(mocked_repo_submodules) == [
        {
            "type": "library",
            "name": "example-repo",
            "version": "https://example.com/repo.git#a816faea7b2a62f26dd0ae763fb657149ca36a1c",
            "purl": (
                "pkg:generic/example-repo?vcs_url=https%3A%2F%2Fexample.com%2Frepo.git%"
                "40a816faea7b2a62f26dd0ae763fb657149ca36a1c"
            ),
        }
    ]


def test_submodules_request_json_deps(mocked_repo_submodules):
    assert get_submodules_request_json_deps(mocked_repo_submodules) == [
        {
            "type": "git-submodule",
            "name": "example-repo",
            "path": "example-repo",
            "version": "https://example.com/repo.git#a816faea7b2a62f26dd0ae763fb657149ca36a1c",
        }
    ]
