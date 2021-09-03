"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os.path
from collections import Counter

from atomic_reactor.constants import (
    PLUGIN_RESOLVE_REMOTE_SOURCE,
    REMOTE_SOURCE_DIR,
    REMOTE_SOURCE_JSON_FILENAME,
    REMOTE_SOURCE_TARBALL_FILENAME,
)
from atomic_reactor.config import get_koji_session, get_cachito_session
from atomic_reactor.utils.koji import get_koji_task_owner
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.build_orchestrate_build import override_build_kwarg
from atomic_reactor.util import get_build_json, is_scratch_build


class ResolveRemoteSourcePlugin(PreBuildPlugin):
    """Initiate a new Cachito request for sources

    This plugin will read the remote_sources configuration from
    container.yaml in the git repository, use it to make a request
    to Cachito, and wait for the request to complete.
    """

    key = PLUGIN_RESOLVE_REMOTE_SOURCE
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, dependency_replacements=None):
        """
        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param dependency_replacements: list<str>, dependencies for the cachito fetched artifact to
        be replaced. Must be of the form pkg_manager:name:version[:new_name]
        """
        super(ResolveRemoteSourcePlugin, self).__init__(tasker, workflow)
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

        remote_sources_workers_params = []
        remote_sources_output = []
        user = self.get_koji_user()
        self.log.info('Using user "%s" for cachito request', user)
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
                self.process_request(request, name, remote_sources_workers_params,
                                     remote_sources_output)

        else:
            open_request = self.cachito_session.request_sources(
                    user=user,
                    dependency_replacements=self._dependency_replacements,
                    **self.single_remote_source_params
            )
            completed_request = self.cachito_session.wait_for_request(open_request)
            self.process_request(completed_request, None, remote_sources_workers_params,
                                 remote_sources_output)

        self.set_worker_params(remote_sources_workers_params)

        return remote_sources_output

    def set_worker_params(self, remote_sources):
        for remote_source in remote_sources:
            build_args = {}
            env_vars = self.cachito_session.get_request_env_vars(remote_source['request_id'])

            for env_var, value_info in env_vars.items():
                build_arg_value = value_info['value']
                kind = value_info['kind']
                if kind == 'path':
                    name = remote_source['name'] or ''
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

            remote_source['build_args'] = build_args
        override_build_kwarg(self.workflow, 'remote_sources', remote_sources)

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

        data.update({k: source_request.get(k, []) for k in optional})

        return data

    def get_koji_user(self):
        unknown_user = self.workflow.conf.cachito.get('unknown_user', 'unknown_user')
        try:
            metadata = get_build_json()['metadata']
        except KeyError:
            msg = 'Unable to get koji user: No build metadata'
            self.log.warning(msg)
            return unknown_user

        try:
            koji_task_id = int(metadata.get('labels').get('koji-task-id'))
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

    def process_request(self, source_request, name, remote_sources_workers_params,
                        remote_sources_output):

        remote_source_json = self.source_request_to_json(source_request)
        remote_source_worker_params = {
            "build_args": None,
            "configs": remote_source_json.get('configuration_files'),
            "request_id": self.cachito_session._get_request_id(source_request),
            "url": self.cachito_session.assemble_download_url(source_request),
            "name": name,
        }
        remote_sources_workers_params.append(remote_source_worker_params)

        if name:
            tarball_filename = f"remote-source-{name}.tar.gz"
            json_filename = f"remote-source-{name}.json"
        else:
            tarball_filename = REMOTE_SOURCE_TARBALL_FILENAME
            json_filename = REMOTE_SOURCE_JSON_FILENAME

        tarball_dest_path = self.cachito_session.download_sources(
            source_request,
            dest_dir=self.workflow.source.workdir,
            dest_filename=tarball_filename,
        )

        remote_source = {
            "name": remote_source_worker_params["name"],
            "url": remote_source_worker_params["url"],
            "remote_source_json": {
                "json": remote_source_json,
                "filename": json_filename,
            },
            "remote_source_tarball": {
                "filename": tarball_filename,
                "path": tarball_dest_path,
            },
        }
        remote_sources_output.append(remote_source)
