"""
Copyright (c) 2024 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.utils.cachi2 import (
    convert_SBOM_to_ICM,
    remote_source_to_cachi2,
    gen_dependency_from_sbom_component,
    generate_request_json,
)

import pytest


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
])
def test_remote_source_to_cachi2_conversion(input_remote_source, expected_cachi2):
    """Test conversion of remote_source (cachito) configuration from container yaml
    into cachi2 params"""
    assert remote_source_to_cachi2(input_remote_source) == expected_cachi2


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
                "2f0ce1d7b1f8b35572d919428b965285a69583f6"),
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
            "version": "https://example.com/pkg",
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

    remote_source_env_json = {
        "GOCACHE": {
            "kind": "path",
            "value": "deps/gomod"
        },
    }

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
