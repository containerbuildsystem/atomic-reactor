"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os.path
import re

from atomic_reactor.constants import (
    PLUGIN_RESOLVE_REMOTE_SOURCE, REMOTE_SOURCE_DIR, CACHITO_ENV_ARG_ALIAS, CACHITO_ENV_FILENAME)
from atomic_reactor.utils.koji import get_koji_task_owner
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.build_orchestrate_build import override_build_kwarg
from atomic_reactor.plugins.pre_reactor_config import (
    get_cachito, get_cachito_session, get_koji_session)
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
        remote_source_url = self.cachito_session.assemble_download_url(source_request)
        remote_source_conf_url = remote_source_json.get('configuration_files')
        remote_source_icm_url = remote_source_json.get('content_manifest')
        self.set_worker_params(source_request, remote_source_url, remote_source_conf_url,
                               remote_source_icm_url)

        dest_dir = self.workflow.source.workdir
        dest_path = self.cachito_session.download_sources(source_request, dest_dir=dest_dir)

        return {
            # Annotations to be added to the current Build object
            'annotations': {'remote_source_url': remote_source_url},
            # JSON representation of the remote source request
            'remote_source_json': remote_source_json,
            # Local path to the remote source archive
            'remote_source_path': dest_path,
        }

    def set_worker_params(self, source_request, remote_source_url, remote_source_conf_url,
                          remote_source_icm_url):
        build_args = {}
        # This matches values such as 'deps/gomod' but not 'true'
        rel_path_regex = re.compile(r'^[^/]+/[^/]+(?:/[^/]+)*$')
        for env_var, value in source_request.get('environment_variables', {}).items():
            # Turn the environment variables that are relative paths into absolute paths that
            # represent where the remote sources are copied to during the build process.
            if re.match(rel_path_regex, value):
                abs_path = os.path.join(REMOTE_SOURCE_DIR, value)
                self.log.debug(
                    'Setting the Cachito environment variable "%s" to the absolute path "%s"',
                    env_var,
                    abs_path,
                )
                build_args[env_var] = abs_path
            else:
                build_args[env_var] = value

        # Alias for absolute path to cachito.env script added into buildargs
        build_args[CACHITO_ENV_ARG_ALIAS] = os.path.join(REMOTE_SOURCE_DIR, CACHITO_ENV_FILENAME)

        override_build_kwarg(self.workflow, 'remote_source_url', remote_source_url)
        override_build_kwarg(self.workflow, 'remote_source_build_args', build_args)
        override_build_kwarg(self.workflow, 'remote_source_configs', remote_source_conf_url)
        override_build_kwarg(self.workflow, 'remote_source_icm_url', remote_source_icm_url)

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
