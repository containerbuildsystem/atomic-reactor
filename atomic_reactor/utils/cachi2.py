# Copyright (c) 2024 Red Hat, Inc
# All rights reserved.
#
# This software may be modified and distributed under the terms
# of the BSD license. See the LICENSE file for details.

"""
Utils to help to integrate with cachi2 CLI tool
"""

from typing import Any, Callable, Dict, Optional, Tuple, List
from pathlib import Path
import os.path

from packageurl import PackageURL


def validate_paths(repo_path: Path, remote_sources_packages: dict) -> None:
    """Paths must be relative and within cloned repo"""
    def is_path_ok(path_string):
        path = Path(path_string)
        if path.is_absolute():
            return False

        # using real repo to properly resolve and block bad symlinks
        full_path = (repo_path/path).resolve()

        # using commonpath to be compatible with py3.8
        if os.path.commonpath((full_path, repo_path)) != str(repo_path):
            return False

        return True

    for pkg_mgr, options in remote_sources_packages.items():
        if not options:
            continue

        for option in options:
            for key, val in option.items():
                if key == "path":
                    if not is_path_ok(val):
                        raise ValueError(
                            f"{pkg_mgr}:{key}: path '{val}' must be relative "
                            "within remote source repository"
                        )
                elif (
                    pkg_mgr == "pip" and
                    key in ("requirements_files", "requirements_build_files")
                ):
                    for v in val:
                        if not is_path_ok(v):
                            raise ValueError(
                                f"{pkg_mgr}:{key}: path '{v}' must be relative "
                                "within remote source repository"
                            )
                else:
                    raise ValueError(f"unexpected key '{key}' in '{pkg_mgr}' config")


def normalize_gomod_pkg_manager(remote_source: Dict[str, Any]):
    """Cachito compatibility, empty/undefined pkg_managers means gomod.
    Replace it to explicitly use gomod

    Function does in-place change.
    """
    pkg_managers = remote_source.get("pkg_managers")
    if pkg_managers is None:
        # Cachito behavior, missing pkg_managers means to use gomod
        pkg_managers = ["gomod"]
    remote_source["pkg_managers"] = pkg_managers


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
    pkg_managers_map = {
        "rubygems": "bundler"  # renamed in cachi2
    }

    removed_flags = {"include-git-dir", "remove-unsafe-symlinks"}
    removed_pkg_managers = {"git-submodule"}

    cachi2_flags = sorted(
        set(remote_source.get("flags", [])) - removed_flags
    )
    cachi2_packages = []

    normalize_gomod_pkg_manager(remote_source)

    pkg_managers = remote_source["pkg_managers"]

    for pkg_manager in pkg_managers:
        if pkg_manager in removed_pkg_managers:
            continue

        # if pkg manager has different name in cachi2 update it
        pkg_manager = pkg_managers_map.get(pkg_manager, pkg_manager)

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


def gen_dependency_from_sbom_component(sbom_dep: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Generate a single request.json dependency from a SBOM component

    Dependency type is derived from purl.
    Version is decided on how Cachito would do it.
    """
    # we need to detect type from purl, this is just heuristics,
    # we cannot reliably construct type from purl
    purl = PackageURL.from_string(sbom_dep["purl"])
    heuristic_type = purl.type or "unknown"  # for unknown types, reuse what's in purl type
    # types supported by cachito/cachi2
    purl_type_matchers: Tuple[Tuple[Callable[[PackageURL], bool], str], ...] = (
        (lambda p: p.type == "golang" and p.qualifiers.get("type", "") == "module", "gomod"),
        (lambda p: p.type == "golang", "go-package"),
        (lambda p: p.type == "npm", "npm"),
        (lambda p: p.type == "pypi", "pip"),
        (lambda p: p.type == "rpm", "rpm"),
        (lambda p: p.type == "gem", "rubygems"),
        (lambda p: p.type == "cargo", "cargo"),
    )

    for matcher, request_type in purl_type_matchers:
        if matcher(purl):
            heuristic_type = request_type
            break

    pkg_dot_path = ("golang", "gem")

    version = (
        # for non-registry dependencies cachito uses URL as version
        purl.qualifiers.get("vcs_url") or
        purl.qualifiers.get("download_url") or
        # for local dependencies Cachito uses path as version
        (f"./{purl.subpath}" if purl.subpath and purl.type in pkg_dot_path else None) or
        (f"file:{purl.subpath}" if purl.subpath and purl.type not in pkg_dot_path else None) or
        # version is mainly for dependencies from pkg registries
        sbom_dep.get("version")
        # returns None if version cannot be determined
    )

    if version and purl.subpath and not version.endswith(purl.subpath):
        # include subpath into vcs or download url to get exact location of dependency
        # used mainly for vendored deps
        version = f"{version}#{purl.subpath}"

    res = {
        "name": sbom_dep["name"],
        "replaces": None,  # it's always None, replacements aren't supported by cachi2
        "type": heuristic_type,
        "version": version,
    }

    # dev package definition
    # currently only NPM
    if any(p["name"] == "cdx:npm:package:development" and p["value"] == "true"
           for p in sbom_dep.get("properties", [])):
        res["dev"] = True

    return res


def generate_request_json(
    remote_source: Dict[str, Any], remote_source_sbom: Dict[str, Any],
    remote_source_env_json: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Generates Cachito like request.json

    Cachito does provide request.json, for backward compatibility
    as some tools are depending on it, we have to generate also request.json from cachi2
    """

    res = {
        "dependencies": [
            gen_dependency_from_sbom_component(dep)
            for dep in remote_source_sbom["components"]
        ],
        "pkg_managers": remote_source.get("pkg_managers", []),
        "ref": remote_source["ref"],
        "repo": remote_source["repo"],
        "environment_variables": {env['name']: env["value"] for env in remote_source_env_json},
        "flags": remote_source.get("flags", []),
        "packages": [],  # this will be always empty cachi2 doesn't provide nested deps
    }
    return res


def clone_only(remote_source: Dict[str, Any]) -> bool:
    """Determine if only cloning is required without cachi2 run"""

    pkg_managers = remote_source.get("pkg_managers")

    if pkg_managers is not None and len(pkg_managers) == 0:
        return True

    return False
