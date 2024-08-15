"""
Copyright (c) 2024 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.utils.cachi2 import (
    convert_SBOM_to_ICM,
    remote_source_to_cachi2
)

import pytest


@pytest.mark.parametrize(('input_remote_source', 'expected_cachi2'), [
    pytest.param(
        {"pkg_managers": ["gomod"]},
        {"flags": [], "packages": [{"path": ".", "type": "gomod"}]},
        id="pkg_manager_plain",
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
