"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os.path

from atomic_reactor.constants import (
    PLUGIN_RESOLVE_REMOTE_SOURCE,
    REMOTE_SOURCE_DIR,
    REMOTE_SOURCE_JSON_FILENAME,
    REMOTE_SOURCE_TARBALL_FILENAME,
)
from atomic_reactor.utils.koji import get_koji_task_owner
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.build_orchestrate_build import override_build_kwarg
from atomic_reactor.plugins.pre_reactor_config import (
    get_cachito, get_cachito_session, get_koji_session, get_allow_multiple_remote_sources)
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
        try:
            get_cachito(self.workflow)
        except KeyError:
            self.log.info('Aborting plugin execution: missing Cachito configuration')
            return

        if (not get_allow_multiple_remote_sources(self.workflow)
                and self.workflow.source.config.remote_sources):
            raise ValueError('Multiple remote sources are not supported, use single '
                             'remote source in container.yaml')

        remote_source_params = self.workflow.source.config.remote_source
        if not remote_source_params:
            self.log.info('Aborting plugin execution: missing remote_source configuration')
            return

        if self._dependency_replacements and not is_scratch_build(self.workflow):
            raise ValueError('Cachito dependency replacements are only allowed for scratch builds')

        user = self.get_koji_user()
        self.log.info('Using user "%s" for cachito request', user)

        source_request = self.cachito_session.request_sources(
            user=user,
            dependency_replacements=self._dependency_replacements,
            **remote_source_params
        )
        source_request = self.cachito_session.wait_for_request(source_request)

        remote_source_json = self.source_request_to_json(source_request)
        remote_sources = [
            {
                "build_args": None,
                "configs": remote_source_json.get('configuration_files'),
                "request_id": self.cachito_session._get_request_id(source_request),
                "url": self.cachito_session.assemble_download_url(source_request),
                "name": None,
            }
        ]
        self.set_worker_params(remote_sources)

        dest_dir = self.workflow.source.workdir
        dest_path = self.cachito_session.download_sources(source_request, dest_dir=dest_dir)

        return [
            {
                "name": remote_sources[0]["name"],
                "url": remote_sources[0]["url"],
                "remote_source_json": {
                    "json": remote_source_json,
                    "filename": REMOTE_SOURCE_JSON_FILENAME,
                },
                "remote_source_tarball": {
                    "filename": REMOTE_SOURCE_TARBALL_FILENAME,
                    "path": dest_path,
                },
            }
        ]

    def set_worker_params(self, remote_sources):
        build_args = {}
        env_vars = self.cachito_session.get_request_env_vars(remote_sources[0]['request_id'])

        for env_var, value_info in env_vars.items():
            build_arg_value = value_info['value']
            kind = value_info['kind']
            if kind == 'path':
                build_arg_value = os.path.join(REMOTE_SOURCE_DIR, value_info['value'])
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

        remote_sources[0]['build_args'] = build_args
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
        unknown_user = get_cachito(self.workflow).get('unknown_user', 'unknown_user')
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

        koji_session = get_koji_session(self.workflow)
        return get_koji_task_owner(koji_session, koji_task_id).get('name', unknown_user)

    @property
    def cachito_session(self):
        if not self._cachito_session:
            self._cachito_session = get_cachito_session(self.workflow)
        return self._cachito_session
