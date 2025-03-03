"""
Copyright (c) 2024 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import base64
import functools
import json
import os.path
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from shutil import copytree
from typing import Any, Optional, List, Dict

import reflink

from atomic_reactor.constants import (
    CACHITO_ENV_ARG_ALIAS,
    CACHITO_ENV_FILENAME,
    HERMETO_BUILD_DIR,
    HERMETO_ENV_JSON,
    PLUGIN_HERMETO_INIT,
    PLUGIN_HERMETO_POSTPROCESS,
    HERMETO_BUILD_CONFIG_JSON,
    REMOTE_SOURCE_DIR,
    REMOTE_SOURCE_JSON_FILENAME,
    REMOTE_SOURCE_TARBALL_FILENAME,
    REMOTE_SOURCE_JSON_CONFIG_FILENAME,
    REMOTE_SOURCE_JSON_ENV_FILENAME,
)
from atomic_reactor.dirs import BuildDir, reflink_copy
from atomic_reactor.plugin import Plugin

from atomic_reactor.utils.hermeto import generate_request_json


@dataclass(frozen=True)
class HermetoRemoteSource:
    """Represents a processed remote source.

    name: the name that identifies this remote source (if multiple remote sources were used)
    json_data: subset of the JSON representation of the Cachito request (source_request_to_json)
    build_args: environment variables for this remote source
    tarball_path: the path of the tarball downloaded from Cachito
    """

    name: Optional[str]
    json_data: dict
    json_env_data: List[Dict[str, str]]
    json_config_data: List[Dict[str, str]]
    tarball_path: Path
    sources_path: Path

    @classmethod
    def tarball_filename(cls, name: Optional[str]):
        if name:
            return f"remote-source-{name}.tar.gz"
        else:
            return REMOTE_SOURCE_TARBALL_FILENAME

    @classmethod
    def json_filename(cls, name: Optional[str]):
        if name:
            return f"remote-source-{name}.json"
        else:
            return REMOTE_SOURCE_JSON_FILENAME

    @classmethod
    def json_config_filename(cls, name: Optional[str]):
        if name:
            return f"remote-source-{name}.config.json"
        else:
            return REMOTE_SOURCE_JSON_CONFIG_FILENAME

    @classmethod
    def json_env_filename(cls, name: Optional[str]):
        if name:
            return f"remote-source-{name}.env.json"
        else:
            return REMOTE_SOURCE_JSON_ENV_FILENAME

    @property
    def build_args(self) -> Dict[str, str]:

        return {
            env_var['name']: env_var['value']
            for env_var in self.json_env_data
        }


class HermetoPostprocessPlugin(Plugin):
    """Postprocess Hermeto results

    This plugin will postprocess Hermeto results and provide required metadata
    """

    key = PLUGIN_HERMETO_POSTPROCESS
    is_allowed_to_fail = False
    REMOTE_SOURCE = "unpacked_remote_sources"

    def __init__(self, workflow):
        """
        :param workflow: DockerBuildWorkflow instance

        """
        super(HermetoPostprocessPlugin, self).__init__(workflow)
        self._osbs = None
        self.single_remote_source_params = self.workflow.source.config.remote_source
        self.multiple_remote_sources_params = self.workflow.source.config.remote_sources
        self.init_plugin_data = self.workflow.data.plugins_results.get(PLUGIN_HERMETO_INIT)

    def run(self) -> Optional[List[Dict[str, Any]]]:
        if not self.init_plugin_data:
            self.log.info('Aborting plugin execution: no Hermeto data provided')
            return None

        if not (self.single_remote_source_params or self.multiple_remote_sources_params):
            self.log.info('Aborting plugin execution: missing remote source configuration')
            return None

        processed_remote_sources = self.postprocess_remote_sources()
        self.postprocess_git_submodules_global_sbom()
        self.inject_remote_sources(processed_remote_sources)

        return [
            self.remote_source_to_output(remote_source)
            for remote_source in processed_remote_sources
        ]

    def postprocess_git_submodules_global_sbom(self):
        """atomic-reactor is responsbile for handling git-submodules. Global SBOM must be updated"""
        all_sbom_components = []
        for remote_source in self.init_plugin_data:
            git_submodules = remote_source.get('git_submodules')
            if not git_submodules:
                continue

            all_sbom_components.extend(git_submodules['sbom_components'])

        if not all_sbom_components:
            return

        global_sbom_path = self.workflow.build_dir.path/HERMETO_BUILD_DIR/"bom.json"
        with open(global_sbom_path, 'r') as global_sbom_f:
            global_sbom_data = json.load(global_sbom_f)
        global_sbom_data['components'].extend(all_sbom_components)

        with open(global_sbom_path, 'w') as global_sbom_f:
            json.dump(global_sbom_data, global_sbom_f)
            global_sbom_f.flush()

    def get_remote_source_json_config(self, remote_source_path: str):
        """Process injected remote source and returns

        Hermeto is injecting config updates into app dir,
        we don't want them in source archive, however
        we need them for later rebuild and analysis, to mirror
        Cachito behavior.
        Hermeto creates .build-config.json file that contains paths
        and templates how to modify files.
        Instead of replicating Hermeto templating logic, just use path
        and load already patched file.
        This is internal logic of Hermeto, this may explode violently when
        format of .build-config.json changes. Make sure that Hermeto image
        is tested before promoted to prod.
        """

        json_config: List[Dict] = []
        hermeto_build_config_path = os.path.join(
            remote_source_path, HERMETO_BUILD_CONFIG_JSON)
        if not os.path.exists(hermeto_build_config_path):
            return json_config

        with open(hermeto_build_config_path, 'r') as f:
            hermeto_build_config = json.load(f)

        injected_files = hermeto_build_config.get("project_files", [])
        for injected_file in injected_files:
            path = injected_file["abspath"]  # fail horribly if this is missing

            with open(path, 'rb') as f:
                encoded_data = base64.b64encode(f.read()).decode()  # must be str for JSON

            relpath = os.path.relpath(path, remote_source_path)

            json_config.append(
                {
                    "content": encoded_data,
                    "path": relpath,
                    "type": "base64",
                }
            )
        return json_config

    def postprocess_remote_sources(self) -> List[HermetoRemoteSource]:
        """Process remote source requests and return information about the processed sources."""

        processed_remote_sources = []

        for remote_source in self.init_plugin_data:

            json_env_path = os.path.join(remote_source['source_path'], HERMETO_ENV_JSON)
            with open(json_env_path, 'r') as json_f:
                json_env_data = json.load(json_f)

            sbom_path = os.path.join(remote_source['source_path'], 'bom.json')
            with open(sbom_path, 'r') as sbom_f:
                sbom_data = json.load(sbom_f)

            # request_json must be generated before modifications to sboms are done
            request_json = generate_request_json(
                    remote_source['remote_source'], sbom_data, json_env_data)

            # update metadata with submodules info
            git_submodules = remote_source.get('git_submodules')
            if git_submodules:
                sbom_data['components'].extend(git_submodules['sbom_components'])

                with open(sbom_path, 'w') as sbom_f:
                    json.dump(sbom_data, sbom_f)
                    sbom_f.flush()

                request_json['dependencies'].extend(git_submodules['request_json_dependencies'])

            remote_source_obj = HermetoRemoteSource(
                name=remote_source['name'],
                tarball_path=Path(remote_source['source_path'], 'remote-source.tar.gz'),
                sources_path=Path(remote_source['source_path']),
                json_data=request_json,
                json_env_data=json_env_data,
                json_config_data=self.get_remote_source_json_config(remote_source['source_path'])
            )
            processed_remote_sources.append(remote_source_obj)
        return processed_remote_sources

    def inject_remote_sources(self, remote_sources: List[HermetoRemoteSource]) -> None:
        """Inject processed remote sources into build dirs and add build args to workflow."""
        inject_sources = functools.partial(self.inject_into_build_dir, remote_sources)
        self.workflow.build_dir.for_all_platforms_copy(inject_sources)

        # For single remote_source workflow, inject all build args directly
        if self.single_remote_source_params:
            self.workflow.data.buildargs.update(remote_sources[0].build_args)

        self.add_general_buildargs()

    def inject_into_build_dir(
        self, remote_sources: List[HermetoRemoteSource], build_dir: BuildDir,
    ) -> List[Path]:
        """Inject processed remote sources into a build directory.

        For each remote source, create a dedicated directory, unpack the downloaded tarball
        into it and inject the configuration files and an environment file.

        Return a list of the newly created directories.
        """
        created_dirs = []
        copy_paths = [Path('app'), Path('deps'), Path('bundler')]

        for remote_source in remote_sources:
            dest_dir = build_dir.path.joinpath(self.REMOTE_SOURCE, remote_source.name or "")

            if dest_dir.exists():
                raise RuntimeError(
                    f"Conflicting path {dest_dir.relative_to(build_dir.path)} already exists "
                    "in the dist-git repository"
                )

            dest_dir.mkdir(parents=True)
            created_dirs.append(dest_dir)

            # copy app and deps generated by cachito into build_dir
            copy_method = shutil.copy2
            if reflink.supported_at(dest_dir):
                copy_method = reflink_copy
            self.log.debug(
                "copy method used for Hermeto build_dir_injecting: %s", copy_method.__name__)

            for pth in copy_paths:
                if (remote_source.sources_path/pth).exists():
                    copytree(
                        remote_source.sources_path/pth, dest_dir/pth,
                        symlinks=True, copy_function=copy_method, dirs_exist_ok=True)

            # Create cachito.env file with environment variables received from cachito request
            self.generate_cachito_env_file(dest_dir, remote_source.build_args)

        return created_dirs

    def remote_source_to_output(self, remote_source: HermetoRemoteSource) -> Dict[str, Any]:
        """Convert a processed remote source to a dict to be used as output of this plugin."""

        compat_json = {
            # cachito return data in this format, keep compatibility for koji metadata
            env_var['name']: {
                "value": env_var['value'],
                "kind": "literal",
            }
            for env_var in remote_source.json_env_data
        }

        return {
            "name": remote_source.name,
            "remote_source_json": {
                "json": remote_source.json_data,
                "filename": HermetoRemoteSource.json_filename(remote_source.name),
            },
            "remote_source_json_env": {
                "json": compat_json,
                "filename": HermetoRemoteSource.json_env_filename(remote_source.name),
            },
            "remote_source_json_config": {
                "json": remote_source.json_config_data,
                "filename": HermetoRemoteSource.json_config_filename(remote_source.name),
            },
            "remote_source_tarball": {
                "filename": HermetoRemoteSource.tarball_filename(remote_source.name),
                "path": str(remote_source.tarball_path),
            },
        }

    def generate_cachito_env_file(self, dest_dir: Path, build_args: Dict[str, str]) -> None:
        """
        Generate cachito.env file with exported environment variables received from
        cachito request.

        :param dest_dir: destination directory for env file
        :param build_args: build arguments to set
        """
        self.log.info('Creating %s file with environment variables '
                      'received from Hermeto', CACHITO_ENV_FILENAME)

        # Use dedicated dir in container build workdir for cachito.env
        abs_path = dest_dir / CACHITO_ENV_FILENAME
        with open(abs_path, 'w') as f:
            f.write('#!/bin/bash\n')
            for env_var, value in build_args.items():
                f.write('export {}={}\n'.format(env_var, shlex.quote(value)))

    def add_general_buildargs(self) -> None:
        """Adds general build arguments

        To copy the sources into the build image, Dockerfile should contain
        COPY $REMOTE_SOURCE $REMOTE_SOURCE_DIR
        or COPY $REMOTE_SOURCES $REMOTE_SOURCES_DIR
        """
        if self.multiple_remote_sources_params:
            args_for_dockerfile_to_add = {
                'REMOTE_SOURCES': self.REMOTE_SOURCE,
                'REMOTE_SOURCES_DIR': REMOTE_SOURCE_DIR,
            }
        else:
            args_for_dockerfile_to_add = {
                'REMOTE_SOURCE': self.REMOTE_SOURCE,
                'REMOTE_SOURCE_DIR': REMOTE_SOURCE_DIR,
                CACHITO_ENV_ARG_ALIAS: os.path.join(REMOTE_SOURCE_DIR, CACHITO_ENV_FILENAME),
            }
        self.workflow.data.buildargs.update(args_for_dockerfile_to_add)
