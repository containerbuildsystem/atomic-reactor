"""
Copyright (c) 2024 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
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
    PLUGIN_CACHI2_INIT,
    PLUGIN_CACHI2_POSTPROCESS,
    REMOTE_SOURCE_DIR,
    REMOTE_SOURCE_JSON_FILENAME,
    REMOTE_SOURCE_TARBALL_FILENAME,
    REMOTE_SOURCE_JSON_ENV_FILENAME,
)
from atomic_reactor.dirs import BuildDir, reflink_copy
from atomic_reactor.plugin import Plugin

from atomic_reactor.utils.cachi2 import generate_request_json


@dataclass(frozen=True)
class Cachi2RemoteSource:
    """Represents a processed remote source.

    name: the name that identifies this remote source (if multiple remote sources were used)
    json_data: subset of the JSON representation of the Cachito request (source_request_to_json)
    build_args: environment variables for this remote source
    tarball_path: the path of the tarball downloaded from Cachito
    """

    name: Optional[str]
    json_data: dict
    json_env_data: List[Dict[str, str]]
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


class Cachi2PostprocessPlugin(Plugin):
    """Postprocess cachi2 results

    This plugin will postprocess cachi2 results and provide required metadata
    """

    key = PLUGIN_CACHI2_POSTPROCESS
    is_allowed_to_fail = False
    REMOTE_SOURCE = "unpacked_remote_sources"

    def __init__(self, workflow):
        """
        :param workflow: DockerBuildWorkflow instance

        """
        super(Cachi2PostprocessPlugin, self).__init__(workflow)
        self._osbs = None
        self.single_remote_source_params = self.workflow.source.config.remote_source
        self.multiple_remote_sources_params = self.workflow.source.config.remote_sources
        self.init_plugin_data = self.workflow.data.plugins_results.get(PLUGIN_CACHI2_INIT)

    def run(self) -> Optional[List[Dict[str, Any]]]:
        if not self.init_plugin_data:
            self.log.info('Aborting plugin execution: no cachi2 data provided')
            return None

        if not (self.single_remote_source_params or self.multiple_remote_sources_params):
            self.log.info('Aborting plugin execution: missing remote source configuration')
            return None

        processed_remote_sources = self.postprocess_remote_sources()
        self.inject_remote_sources(processed_remote_sources)

        return [
            self.remote_source_to_output(remote_source)
            for remote_source in processed_remote_sources
        ]

    def postprocess_remote_sources(self) -> List[Cachi2RemoteSource]:
        """Process remote source requests and return information about the processed sources."""

        processed_remote_sources = []

        for remote_source in self.init_plugin_data:

            json_env_path = os.path.join(remote_source['source_path'], 'cachi2.env.json')
            with open(json_env_path, 'r') as json_f:
                json_env_data = json.load(json_f)

            sbom_path = os.path.join(remote_source['source_path'], 'bom.json')
            with open(sbom_path, 'r') as sbom_f:
                sbom_data = json.load(sbom_f)

            remote_source_obj = Cachi2RemoteSource(
                name=remote_source['name'],
                tarball_path=Path(remote_source['source_path'], 'remote-source.tar.gz'),
                sources_path=Path(remote_source['source_path']),
                json_data=generate_request_json(
                    remote_source['remote_source'], sbom_data, json_env_data),
                json_env_data=json_env_data,
            )
            processed_remote_sources.append(remote_source_obj)
        return processed_remote_sources

    def inject_remote_sources(self, remote_sources: List[Cachi2RemoteSource]) -> None:
        """Inject processed remote sources into build dirs and add build args to workflow."""
        inject_sources = functools.partial(self.inject_into_build_dir, remote_sources)
        self.workflow.build_dir.for_all_platforms_copy(inject_sources)

        # For single remote_source workflow, inject all build args directly
        if self.single_remote_source_params:
            self.workflow.data.buildargs.update(remote_sources[0].build_args)

        self.add_general_buildargs()

    def inject_into_build_dir(
        self, remote_sources: List[Cachi2RemoteSource], build_dir: BuildDir,
    ) -> List[Path]:
        """Inject processed remote sources into a build directory.

        For each remote source, create a dedicated directory, unpack the downloaded tarball
        into it and inject the configuration files and an environment file.

        Return a list of the newly created directories.
        """
        created_dirs = []

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
                "copy method used for cachi2 build_dir_injecting: %s", copy_method.__name__)

            copytree(
                remote_source.sources_path, dest_dir,
                symlinks=True, copy_function=copy_method, dirs_exist_ok=True)

            # Create cachito.env file with environment variables received from cachito request
            self.generate_cachito_env_file(dest_dir, remote_source.build_args)

        return created_dirs

    def remote_source_to_output(self, remote_source: Cachi2RemoteSource) -> Dict[str, Any]:
        """Convert a processed remote source to a dict to be used as output of this plugin."""

        return {
            "name": remote_source.name,
            "remote_source_json": {
                "json": remote_source.json_data,
                "filename": Cachi2RemoteSource.json_filename(remote_source.name),
            },
            "remote_source_json_env": {
                "json": remote_source.json_env_data,
                "filename": Cachi2RemoteSource.json_env_filename(remote_source.name),
            },
            "remote_source_tarball": {
                "filename": Cachi2RemoteSource.tarball_filename(remote_source.name),
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
                      'received from cachi2', CACHITO_ENV_FILENAME)

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
