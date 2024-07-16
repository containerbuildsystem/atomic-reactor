"""
Copyright (c) 2024 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.utils.cachi2 import remote_source_to_cachi2

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
