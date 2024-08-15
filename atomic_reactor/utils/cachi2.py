# Copyright (c) 2024 Red Hat, Inc
# All rights reserved.
#
# This software may be modified and distributed under the terms
# of the BSD license. See the LICENSE file for details.

"""
Utils to help to integrate with cachi2 CLI tool
"""

from typing import Any, Dict


def remote_source_to_cachi2(remote_source: Dict[str, Any]) -> Dict[str, Any]:
    """Converts remote source into cachi2 expected params.

    Remote sources were orignally designed for cachito. Cachi2 is not a direct
    fork but has lot of similarities.
    However, some parameters must be updated to be compatible with cachi2.

    Removed flags (OSBS process them):
    * include-git-dir
    * remove-unsafe-symlinks

    Removed pkg-managers (OSBS process them):
    * git-submodule

    """
    removed_flags = {"include-git-dir", "remove-unsafe-symlinks"}
    removed_pkg_managers = {"git-submodule"}

    cachi2_flags = sorted(
        set(remote_source.get("flags", [])) - removed_flags
    )
    cachi2_packages = []

    for pkg_manager in remote_source["pkg_managers"]:
        if pkg_manager in removed_pkg_managers:
            continue

        packages = remote_source.get("packages", {}).get(pkg_manager, [])
        packages = packages or [{"path": "."}]
        for pkg in packages:
            cachi2_packages.append({"type": pkg_manager, **pkg})

    return {"packages": cachi2_packages, "flags": cachi2_flags}


def convert_SBOM_to_ICM(sbom: Dict[str, Any]) -> Dict[str, Any]:
    """Function converts cachi2 SBOM into ICM

    Unfortunately cachi2 doesn't provide all details about dependencies
    and sources, so the ICM can contain only flat structure of everything
    """
    icm = {
        "metadata": {
            "icm_spec": (
                "https://raw.githubusercontent.com/containerbuildsystem/atomic-reactor/"
                "f4abcfdaf8247a6b074f94fa84f3846f82d781c6/atomic_reactor/schemas/"
                "content_manifest.json"
            ),
            "icm_version": 1,
            "image_layer_index": -1
        },
        "image_contents": [],
    }
    icm["image_contents"] = [
        {"purl": comp["purl"]} for comp in sbom["components"]  # type: ignore
    ]
    return icm
