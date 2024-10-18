"""
Copyright (c) 2024 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import json
import os.path
from collections import Counter
from typing import Any, Optional, List, Dict

from osbs.utils import clone_git_repo

from atomic_reactor.constants import (
    PLUGIN_CACHI2_INIT,
    CACHI2_BUILD_DIR,
    CACHI2_BUILD_APP_DIR,
    CACHI2_BUILD_DEP_DIR,
    CACHI2_PKG_OPTIONS_FILE,
    CACHI2_FOR_OUTPUT_DIR_OPT_FILE,
    REMOTE_SOURCE_DIR,
)
from atomic_reactor.plugin import Plugin
from atomic_reactor.util import map_to_user_params
from atomic_reactor.utils.cachi2 import remote_source_to_cachi2


class Cachi2InitPlugin(Plugin):
    """Initiate remote sources for Cachi2

    This plugin will read the remote_sources configuration from
    container.yaml in the git repository, clone them and prepare
    params for Cachi2 execution.
    """

    key = PLUGIN_CACHI2_INIT
    is_allowed_to_fail = False

    args_from_user_params = map_to_user_params("dependency_replacements")

    def __init__(self, workflow, dependency_replacements=None):
        """
        :param workflow: DockerBuildWorkflow instance
        :param dependency_replacements: list<str>, dependencies for the cachito fetched artifact to
        be replaced. Must be of the form pkg_manager:name:version[:new_name]
        """
        super(Cachi2InitPlugin, self).__init__(workflow)
        self._osbs = None
        self.dependency_replacements = dependency_replacements
        self.single_remote_source_params = self.workflow.source.config.remote_source
        self.multiple_remote_sources_params = self.workflow.source.config.remote_sources
        self.remote_sources_root_path = self.workflow.build_dir.path / CACHI2_BUILD_DIR

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
            raise ValueError('Dependency replacements are not supported by Cachi2')

        processed_remote_sources = self.process_remote_sources()

        return processed_remote_sources

    def process_remote_sources(self) -> List[Dict[str, Any]]:
        """Process remote source requests and return information about the processed sources."""

        remote_sources = self.multiple_remote_sources_params
        if self.single_remote_source_params:
            remote_sources = [{
                "name": "",  # name single remote source with generic name
                **self.single_remote_source_params}]

        processed_remote_sources = []

        self.remote_sources_root_path.mkdir()

        for remote_source in remote_sources:
            # single source doesn't have name, fake it for cachi2_run task
            source_name = (
                remote_source["name"] if self.multiple_remote_sources_params
                else "remote-source"
            )
            source_path = self.remote_sources_root_path / source_name
            source_path.mkdir()

            self.write_cachi2_pkg_options(
                remote_source,
                source_path / CACHI2_PKG_OPTIONS_FILE)
            self.write_cachi2_for_output_dir(
                remote_source,
                source_path / CACHI2_FOR_OUTPUT_DIR_OPT_FILE)

            source_path_app = source_path / CACHI2_BUILD_APP_DIR
            source_path_app.mkdir()

            clone_git_repo(
                remote_source["repo"],
                target_dir=source_path_app,
                commit=remote_source["ref"]
            )

            source_path_dep = source_path / CACHI2_BUILD_DEP_DIR
            source_path_dep.mkdir()

            processed_remote_sources.append({
                "source_path": source_path,
                **remote_source,
            })

        return processed_remote_sources

    def verify_multiple_remote_sources_names_are_unique(self):
        names = [remote_source['name'] for remote_source in self.multiple_remote_sources_params]
        duplicate_names = [name for name, count in Counter(names).items() if count > 1]
        if duplicate_names:
            raise ValueError(f'Provided remote sources parameters contain '
                             f'non unique names: {duplicate_names}')

    def write_cachi2_pkg_options(self, remote_source, path):
        with path.open("w") as f:
            json.dump(remote_source_to_cachi2(remote_source), f)

    def write_cachi2_for_output_dir(self, remote_source, path):
        """Write value for --for-output-dir cachi2 option.

        This must be path inside container so users have the right paths to
        use it within image build
        """
        value = os.path.join(REMOTE_SOURCE_DIR, CACHI2_BUILD_DEP_DIR)
        if self.multiple_remote_sources_params:
            value = os.path.join(REMOTE_SOURCE_DIR, remote_source['name'], CACHI2_BUILD_DEP_DIR)

        with open(path, 'w') as f:
            f.write(str(value))
