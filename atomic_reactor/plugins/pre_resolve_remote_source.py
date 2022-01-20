"""
Copyright (c) 2019-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import base64
import functools
import os.path
import shlex
import tarfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict

from atomic_reactor.config import get_koji_session, get_cachito_session
from atomic_reactor.constants import (
    CACHITO_ENV_ARG_ALIAS,
    CACHITO_ENV_FILENAME,
    PLUGIN_RESOLVE_REMOTE_SOURCE,
    REMOTE_SOURCE_DIR,
    REMOTE_SOURCE_JSON_FILENAME,
    REMOTE_SOURCE_TARBALL_FILENAME,
)
from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import is_scratch_build, map_to_user_params
from atomic_reactor.utils.cachito import CFG_TYPE_B64
from atomic_reactor.utils.koji import get_koji_task_owner


@dataclass(frozen=True)
class RemoteSource:
    """Represents a processed remote source.

    id: the ID of the Cachito request for this remote source
    name: the name that identifies this remote source (if multiple remote sources were used)
    json_data: subset of the JSON representation of the Cachito request (source_request_to_json)
    build_args: environment variables for this remote source
    tarball_path: the path of the tarball downloaded from Cachito
    """

    id: int
    name: Optional[str]
    json_data: dict
    build_args: Dict[str, str]
    tarball_path: Path

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


class ResolveRemoteSourcePlugin(PreBuildPlugin):
    """Initiate a new Cachito request for sources

    This plugin will read the remote_sources configuration from
    container.yaml in the git repository, use it to make a request
    to Cachito, and wait for the request to complete.
    """

    key = PLUGIN_RESOLVE_REMOTE_SOURCE
    is_allowed_to_fail = False
    REMOTE_SOURCE = "unpacked_remote_sources"

    args_from_user_params = map_to_user_params("dependency_replacements")

    def __init__(self, workflow, dependency_replacements=None):
        """
        :param workflow: DockerBuildWorkflow instance
        :param dependency_replacements: list<str>, dependencies for the cachito fetched artifact to
        be replaced. Must be of the form pkg_manager:name:version[:new_name]
        """
        super(ResolveRemoteSourcePlugin, self).__init__(workflow)
        self._cachito_session = None
        self._osbs = None
        self._dependency_replacements = self.parse_dependency_replacements(dependency_replacements)
        self.single_remote_source_params = self.workflow.source.config.remote_source
        self.multiple_remote_sources_params = self.workflow.source.config.remote_sources

    def parse_dependency_replacements(self, replacement_strings):
        """Parse dependency_replacements param and return cachito-reaady dependency replacement dict

        param replacement_strings: list<str>, pkg_manager:name:version[:new_name]
        return: list<dict>, cachito formated dependency replacements param
        """
        if not replacement_strings:
            return

        dependency_replacements = []
        for dr_str in replacement_strings:
            pkg_manager, name, version, new_name = (dr_str.split(':', 3) + [None] * 4)[:4]
            if None in [pkg_manager, name, version]:
                raise ValueError('Cachito dependency replacements must be '
                                 '"pkg_manager:name:version[:new_name]". got {}'.format(dr_str))

            dr = {'type': pkg_manager, 'name': name, 'version': version}
            if new_name:
                dr['new_name'] = new_name

            dependency_replacements.append(dr)

        return dependency_replacements

    def run(self):
        if (not self.workflow.conf.allow_multiple_remote_sources
                and self.multiple_remote_sources_params):
            raise ValueError('Multiple remote sources are not enabled, '
                             'use single remote source in container.yaml')

        if not (self.single_remote_source_params or self.multiple_remote_sources_params):
            self.log.info('Aborting plugin execution: missing remote source configuration')
            return

        if not self.workflow.conf.cachito:
            raise RuntimeError('No Cachito configuration defined')

        if self._dependency_replacements and not is_scratch_build(self.workflow):
            raise ValueError('Cachito dependency replacements are only allowed for scratch builds')
        if self._dependency_replacements and self.multiple_remote_sources_params:
            raise ValueError('Cachito dependency replacements are not allowed '
                             'for multiple remote sources')

        processed_remote_sources = self.process_remote_sources()
        self.inject_remote_sources(processed_remote_sources)

        return [
            self.remote_source_to_output(remote_source)
            for remote_source in processed_remote_sources
        ]

    def process_remote_sources(self) -> List[RemoteSource]:
        """Process remote source requests and return information about the processed sources."""
        user = self.get_koji_user()
        self.log.info('Using user "%s" for cachito request', user)

        processed_remote_sources = []

        if self.multiple_remote_sources_params:
            self.verify_multiple_remote_sources_names_are_unique()

            open_requests = {
                remote_source["name"]: self.cachito_session.request_sources(
                    user=user,
                    dependency_replacements=self._dependency_replacements,
                    **remote_source["remote_source"]
                )
                for remote_source in self.multiple_remote_sources_params
            }

            completed_requests = {
                name: self.cachito_session.wait_for_request(request)
                for name, request in open_requests.items()
            }
            for name, request in completed_requests.items():
                processed_remote_sources.append(self.process_request(request, name))

        else:
            open_request = self.cachito_session.request_sources(
                    user=user,
                    dependency_replacements=self._dependency_replacements,
                    **self.single_remote_source_params
            )
            completed_request = self.cachito_session.wait_for_request(open_request)
            processed_remote_sources.append(self.process_request(completed_request, None))

        return processed_remote_sources

    def inject_remote_sources(self, remote_sources: List[RemoteSource]) -> None:
        """Inject processed remote sources into build dirs and add build args to workflow."""
        inject_sources = functools.partial(self.inject_into_build_dir, remote_sources)
        self.workflow.build_dir.for_all_platforms_copy(inject_sources)

        # For single remote_source workflow, inject all build args directly
        if self.single_remote_source_params:
            self.workflow.data.buildargs.update(remote_sources[0].build_args)

        self.add_general_buildargs()

    def inject_into_build_dir(
        self, remote_sources: List[RemoteSource], build_dir: BuildDir,
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

            with tarfile.open(remote_source.tarball_path) as tf:
                tf.extractall(dest_dir)

            config_files = self.cachito_session.get_request_config_files(remote_source.id)
            self.generate_cachito_config_files(dest_dir, config_files)

            # Create cachito.env file with environment variables received from cachito request
            self.generate_cachito_env_file(dest_dir, remote_source.build_args)

        return created_dirs

    def get_buildargs(self, request_id: int, remote_source_name: Optional[str]) -> Dict[str, str]:
        build_args = {}
        env_vars = self.cachito_session.get_request_env_vars(request_id)

        for env_var, value_info in env_vars.items():
            build_arg_value = value_info['value']
            kind = value_info['kind']
            if kind == 'path':
                name = remote_source_name or ''
                build_arg_value = os.path.join(REMOTE_SOURCE_DIR, name, value_info['value'])
                self.log.debug(
                    'Setting the Cachito environment variable "%s" to the absolute path "%s"',
                    env_var,
                    build_arg_value,
                )
                build_args[env_var] = build_arg_value
            elif kind == 'literal':
                self.log.debug(
                    'Setting the Cachito environment variable "%s" to a literal value "%s"',
                    env_var,
                    build_arg_value,
                )
                build_args[env_var] = build_arg_value
            else:
                raise RuntimeError(f'Unknown kind {kind} got from Cachito.')

        return build_args

    def source_request_to_json(self, source_request):
        """Create a relevant representation of the source request"""
        required = ('packages', 'ref', 'repo')
        optional = ('dependencies', 'flags', 'pkg_managers', 'environment_variables',
                    'configuration_files', 'content_manifest')

        data = {}
        try:
            data.update({k: source_request[k] for k in required})
        except KeyError as exc:
            msg = 'Received invalid source request from Cachito: {}'.format(source_request)
            self.log.exception(msg)
            raise ValueError(msg) from exc

        data.update({k: source_request[k] for k in optional if k in source_request})

        return data

    def get_koji_user(self):
        unknown_user = self.workflow.conf.cachito.get('unknown_user', 'unknown_user')
        try:
            koji_task_id = int(self.workflow.user_params.get('koji_task_id'))
        except (ValueError, TypeError, AttributeError):
            msg = 'Unable to get koji user: Invalid Koji task ID'
            self.log.warning(msg)
            return unknown_user

        koji_session = get_koji_session(self.workflow.conf)
        return get_koji_task_owner(koji_session, koji_task_id).get('name', unknown_user)

    @property
    def cachito_session(self):
        if not self._cachito_session:
            self._cachito_session = get_cachito_session(self.workflow.conf)
        return self._cachito_session

    def verify_multiple_remote_sources_names_are_unique(self):
        names = [remote_source['name'] for remote_source in self.multiple_remote_sources_params]
        duplicate_names = [name for name, count in Counter(names).items() if count > 1]
        if duplicate_names:
            raise ValueError(f'Provided remote sources parameters contain '
                             f'non unique names: {duplicate_names}')

    def process_request(self, source_request: dict, name: Optional[str]) -> RemoteSource:
        """Download the tarball for a request and return info about the processed remote source."""
        tarball_filename = RemoteSource.tarball_filename(name)
        dest_dir = str(self.workflow.build_dir.any_platform.path)

        tarball_dest_path = self.cachito_session.download_sources(
            source_request,
            dest_dir=dest_dir,
            dest_filename=tarball_filename,
        )

        build_args = self.get_buildargs(source_request["id"], name)

        remote_source = RemoteSource(
            id=source_request["id"],
            name=name,
            json_data=self.source_request_to_json(source_request),
            build_args=build_args,
            tarball_path=Path(tarball_dest_path),
        )
        return remote_source

    def remote_source_to_output(self, remote_source: RemoteSource) -> dict:
        """Convert a processed remote source to a dict to be used as output of this plugin."""
        download_url = self.cachito_session.assemble_download_url(remote_source.id)
        json_filename = RemoteSource.json_filename(remote_source.name)

        return {
            "id": remote_source.id,
            "name": remote_source.name,
            "url": download_url,
            "remote_source_json": {
                "json": remote_source.json_data,
                "filename": json_filename,
            },
            "remote_source_tarball": {
                "filename": remote_source.tarball_path.name,
                "path": str(remote_source.tarball_path),
            },
        }

    def generate_cachito_config_files(self, dest_dir: Path, config_files: List[dict]) -> None:
        """Inject cachito provided configuration files

        :param dest_dir: destination directory for config files
        :param config_files: configuration files from cachito
        """
        for config in config_files:
            config_path = dest_dir / config['path']
            if config['type'] == CFG_TYPE_B64:
                data = base64.b64decode(config['content'])
                config_path.write_bytes(data)
            else:
                err_msg = "Unknown cachito configuration file data type '{}'".format(config['type'])
                raise ValueError(err_msg)

            config_path.chmod(0o444)

    def generate_cachito_env_file(self, dest_dir: Path, build_args: Dict[str, str]) -> None:
        """
        Generate cachito.env file with exported environment variables received from
        cachito request.

        :param dest_dir: destination directory for env file
        :param build_args: build arguments to set
        """
        self.log.info('Creating %s file with environment variables '
                      'received from cachito request', CACHITO_ENV_FILENAME)

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
