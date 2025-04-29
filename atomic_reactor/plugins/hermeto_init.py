"""
Copyright (c) 2024 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import json
from collections import Counter
from typing import Any, Optional, List, Dict
from pathlib import Path

import git
from osbs.utils import clone_git_repo

from atomic_reactor.constants import (
    PLUGIN_HERMETO_INIT,
    HERMETO_BUILD_DIR,
    HERMETO_BUILD_APP_DIR,
    HERMETO_PKG_OPTIONS_FILE,
    HERMETO_FOR_OUTPUT_DIR_OPT_FILE,
    HERMETO_INCLUDE_GIT_DIR_FILE,
    HERMETO_SINGLE_REMOTE_SOURCE_NAME,
    HERMETO_SBOM_JSON,
    HERMETO_ENV_JSON,
    REMOTE_SOURCE_DIR,
)
from atomic_reactor.plugin import Plugin
from atomic_reactor.util import map_to_user_params
from atomic_reactor.utils.hermeto import (
    remote_source_to_hermeto, clone_only, validate_paths,
    normalize_gomod_pkg_manager, enforce_sandbox,
    has_git_submodule_manager, update_submodules,
    get_submodules_sbom_components, get_submodules_request_json_deps,
)


class HermetoInitPlugin(Plugin):
    """Initiate remote sources for Hermeto

    This plugin will read the remote_sources configuration from
    container.yaml in the git repository, clone them and prepare
    params for Hermeto execution.
    """

    key = PLUGIN_HERMETO_INIT
    is_allowed_to_fail = False

    args_from_user_params = map_to_user_params("dependency_replacements")

    def __init__(self, workflow, dependency_replacements=None):
        """
        :param workflow: DockerBuildWorkflow instance
        :param dependency_replacements: list<str>, dependencies for the cachito fetched artifact to
        be replaced. Must be of the form pkg_manager:name:version[:new_name]
        """
        super(HermetoInitPlugin, self).__init__(workflow)
        self._osbs = None
        self.dependency_replacements = dependency_replacements
        self.single_remote_source_params = self.workflow.source.config.remote_source
        self.multiple_remote_sources_params = self.workflow.source.config.remote_sources
        self.remote_sources_root_path = self.workflow.build_dir.path / HERMETO_BUILD_DIR

    def run(self) -> Optional[List[Dict[str, Any]]]:
        if (not self.workflow.conf.allow_multiple_remote_sources
                and self.multiple_remote_sources_params):
            raise ValueError('Multiple remote sources are not enabled, '
                             'use single remote source in container.yaml')

        if not (self.single_remote_source_params or self.multiple_remote_sources_params):
            self.log.info('Aborting plugin execution: missing remote source configuration')
            return None

        if self.multiple_remote_sources_params:
            self.verify_multiple_remote_sources_names_are_unique()

        if self.dependency_replacements:
            raise ValueError('Dependency replacements are not supported by Hermeto')

        processed_remote_sources = self.process_remote_sources()

        return processed_remote_sources

    def write_metadata(self, remote_source_path: Path):
        """Step when OSBS only is doing resolution without Hermeto.

        Generate and write SBOM and env file for next plugins
        """
        # generate empty SBOM
        sbom_path = remote_source_path / HERMETO_SBOM_JSON
        with open(sbom_path, "w") as f:
            json.dump(
                {
                    "bomFormat": "CycloneDX",
                    "components": [],
                },
                f
            )

        # generate empty envs
        env_path = remote_source_path / HERMETO_ENV_JSON
        with open(env_path, "w") as f:
            json.dump([], f)

    def process_include_git_dir_flag(self, remote_source: Dict, source_path: Path):
        """Process remote source include-git-dir flag

        Hermeto needs git metadata, so git dir must be removed after in tekton run step.

        If include-git-dir is specified, let know to hermeto run step that git should be kept
        """
        if "include-git-dir" in (remote_source.get("flags") or []):
            (source_path/HERMETO_INCLUDE_GIT_DIR_FILE).touch()

    def process_remote_sources(self) -> List[Dict[str, Any]]:
        """Process remote source requests and return information about the processed sources."""

        remote_sources = self.multiple_remote_sources_params
        if self.single_remote_source_params:
            remote_sources = [{
                "name": None,
                "remote_source": self.single_remote_source_params}]

        processed_remote_sources = []

        self.remote_sources_root_path.mkdir()

        for remote_source in remote_sources:
            # single source doesn't have name, fake it for hermeto_run task
            source_name = (
                remote_source["name"] if self.multiple_remote_sources_params
                else HERMETO_SINGLE_REMOTE_SOURCE_NAME
            )

            normalize_gomod_pkg_manager(remote_source['remote_source'])

            self.log.info("Initializing remote source %s", source_name)
            source_path = self.remote_sources_root_path / source_name
            source_path.mkdir()

            remote_source_data = remote_source["remote_source"]

            source_path_app = source_path / HERMETO_BUILD_APP_DIR
            source_path_app.mkdir()

            self.clone_remote_source(
                remote_source_data["repo"],
                source_path_app,
                remote_source_data["ref"]
            )

            if has_git_submodule_manager(remote_source_data):
                update_submodules(source_path_app)
                repo = git.Repo(str(source_path_app))
                git_submodules = {
                    "sbom_components": get_submodules_sbom_components(repo),
                    "request_json_dependencies": get_submodules_request_json_deps(repo)
                }
                remote_source["git_submodules"] = git_submodules

            remove_unsafe_symlinks = False
            flags = remote_source_data.get("flags") or []
            if "remove-unsafe-symlinks" in flags:
                remove_unsafe_symlinks = True

            enforce_sandbox(
                source_path_app,
                remove_unsafe_symlinks,
            )

            validate_paths(source_path_app, remote_source_data.get("packages", {}))

            if clone_only(remote_source_data):
                # OSBS is doing all work here
                self.write_metadata(source_path)
            else:
                # write Hermeto files only when Hermeto should run
                self.write_hermeto_pkg_options(
                    remote_source_data,
                    source_path / HERMETO_PKG_OPTIONS_FILE)
                self.write_hermeto_for_output_dir(
                    source_name,
                    source_path / HERMETO_FOR_OUTPUT_DIR_OPT_FILE)

            self.process_include_git_dir_flag(remote_source_data, source_path)

            processed_remote_sources.append({
                "source_path": str(source_path),
                **remote_source,
            })

        return processed_remote_sources

    def clone_remote_source(self, repo: str, target_dir: Path, commit: str):
        self.log.debug("Cloning %s at %s into %s", repo, commit, target_dir)
        clone_git_repo(
            repo,
            target_dir,
            commit
        )

    def verify_multiple_remote_sources_names_are_unique(self):
        names = [remote_source['name'] for remote_source in self.multiple_remote_sources_params]
        duplicate_names = [name for name, count in Counter(names).items() if count > 1]
        if duplicate_names:
            raise ValueError(f'Provided remote sources parameters contain '
                             f'non unique names: {duplicate_names}')

    def write_hermeto_pkg_options(self, remote_source: Dict[str, Any], path: Path):
        """Write Hermeto package options into file"""
        with path.open("w") as f:
            json.dump(remote_source_to_hermeto(remote_source), f)

    def write_hermeto_for_output_dir(self, remote_source_name: str, path: Path):
        """Write value for --for-output-dir Hermeto option.

        This must be path inside container so users have the right paths to
        use it within image build
        """
        value = Path(REMOTE_SOURCE_DIR)
        if self.multiple_remote_sources_params:
            value = value / remote_source_name

        with open(path, 'w') as f:
            f.write(str(value))
