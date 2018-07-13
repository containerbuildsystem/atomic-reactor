"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals

from datetime import datetime, timedelta
import os
import yaml
from collections import defaultdict

from atomic_reactor.constants import (PLUGIN_KOJI_PARENT_KEY, PLUGIN_RESOLVE_COMPOSES_KEY,
                                      REPO_CONTENT_SETS_CONFIG, PLUGIN_BUILD_ORCHESTRATE_KEY,
                                      PLUGIN_CHECK_AND_SET_PLATFORMS_KEY, BASE_IMAGE_KOJI_BUILD)

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.build_orchestrate_build import override_build_kwarg
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild
from atomic_reactor.plugins.pre_reactor_config import (get_config,
                                                       get_odcs_session,
                                                       get_koji_session, get_koji)

ODCS_DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
MINIMUM_TIME_TO_EXPIRE = timedelta(hours=2).total_seconds()
# flag to let ODCS see hidden pulp repos
UNPUBLISHED_REPOS = 'include_unpublished_pulp_repos'


class ResolveComposesPlugin(PreBuildPlugin):
    """Request a new, or use existing, ODCS compose

    This plugin will read the configuration in git repository
    and request ODCS to create a corresponding yum repository.
    """

    key = PLUGIN_RESOLVE_COMPOSES_KEY
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow,
                 odcs_url=None,
                 odcs_insecure=False,
                 odcs_openidc_secret_path=None,
                 odcs_ssl_secret_path=None,
                 koji_target=None,
                 koji_hub=None,
                 koji_ssl_certs_dir=None,
                 signing_intent=None,
                 compose_ids=tuple(),
                 minimum_time_to_expire=MINIMUM_TIME_TO_EXPIRE,
                 ):
        """
        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param odcs_url: URL of ODCS (On Demand Compose Service)
        :param odcs_insecure: If True, don't check SSL certificates for `odcs_url`
        :param odcs_openidc_secret_path: directory to look in for a `token` file
        :param odcs_ssl_secret_path: directory to look in for `cert` file - a PEM file
                                     containing both cert and key
        :param koji_target: str, contains build tag to be used when requesting compose from "tag"
        :param koji_hub: str, koji hub (xmlrpc), required if koji_target is used
        :param koji_ssl_certs_dir: str, path to "cert", and "serverca"
                                   used when Koji's identity certificate is not trusted
        :param signing_intent: override the signing intent from git repo configuration
        :param compose_ids: use the given compose_ids instead of requesting a new one
        :param minimum_time_to_expire: int, used in deciding when to extend compose's time
                                       to expire in seconds
        """
        super(ResolveComposesPlugin, self).__init__(tasker, workflow)

        if signing_intent and compose_ids:
            raise ValueError('signing_intent and compose_ids cannot be used at the same time')

        self.signing_intent = signing_intent
        self.compose_ids = compose_ids
        self.odcs_fallback = {
            'api_url': odcs_url,
            'insecure': odcs_insecure,
            'auth': {
                'ssl_certs_dir': odcs_ssl_secret_path,
                'openidc_dir': odcs_openidc_secret_path
            }
        }

        self.koji_target = koji_target
        self.koji_fallback = {
            'hub_url': koji_hub,
            'auth': {
                'ssl_certs_dir': koji_ssl_certs_dir
            }
        }
        if koji_target:
            if not get_koji(self.workflow, self.koji_fallback)['hub_url']:
                raise ValueError('koji_hub is required when koji_target is used')

        self.minimum_time_to_expire = minimum_time_to_expire

        self._koji_session = None
        self._odcs_client = None
        self.odcs_config = None
        self.compose_config = None
        self.composes_info = None
        self._parent_signing_intent = None

    def run(self):
        try:
            self.adjust_for_autorebuild()
            self.read_configs()
            self.adjust_compose_config()
            self.request_compose_if_needed()
            self.wait_for_composes()
            self.resolve_signing_intent()
            self.forward_composes()
            return self.make_result()
        except SkipResolveComposesPlugin as abort_exc:
            self.log.info('Aborting plugin execution: %s', abort_exc)

    def adjust_for_autorebuild(self):
        """Ignore pre-filled signing_intent and compose_ids for autorebuids

        Auto rebuilds are expected to use a known configuration. The parameters
        signing_intent and compose_ids are meant for one-off build calls. This
        method ensure that these parameters are ignored for autorebuilds.
        """
        if not is_rebuild(self.workflow):
            return

        if self.signing_intent:
            self.log.info('Autorebuild detected: Ignoring signing_intent plugin parameter')
            self.signing_intent = None

        if self.compose_ids:
            self.log.info('Autorebuild detected: Ignoring compose_ids plugin parameter')
            self.compose_ids = tuple()

    def get_arches(self):
        platforms = self.workflow.prebuild_results.get(PLUGIN_CHECK_AND_SET_PLATFORMS_KEY)
        if platforms:
            return [platform for platform in platforms]

        # Fallback to build_orchestrate_build args if check_and_set_platforms didn't run
        for plugin in self.workflow.buildstep_plugins_conf:
            if plugin['name'] == PLUGIN_BUILD_ORCHESTRATE_KEY:
                return plugin['args']['platforms']

    def read_configs(self):
        self.odcs_config = get_config(self.workflow).get_odcs_config()
        if not self.odcs_config:
            raise SkipResolveComposesPlugin('ODCS config not found')

        data = self.workflow.source.config.compose
        if not data and not self.compose_ids:
            raise SkipResolveComposesPlugin('"compose" config not set and compose_ids not given')

        workdir = self.workflow.source.get_build_file_path()[1]
        file_path = os.path.join(workdir, REPO_CONTENT_SETS_CONFIG)
        pulp_data = None
        if os.path.exists(file_path):
            with open(file_path) as f:
                pulp_data = yaml.safe_load(f) or {}

        arches = self.get_arches()

        self.compose_config = ComposeConfig(data, pulp_data, self.odcs_config, arches=arches)

    def adjust_compose_config(self):
        if self.signing_intent:
            self.compose_config.set_signing_intent(self.signing_intent)

        if self.koji_target:
            target_info = self.koji_session.getBuildTarget(self.koji_target, strict=True)
            self.compose_config.koji_tag = target_info['build_tag_name']

        self.adjust_signing_intent_from_parent()

    def adjust_signing_intent_from_parent(self):
        plugin_result = self.workflow.prebuild_results.get(PLUGIN_KOJI_PARENT_KEY)
        if not plugin_result:
            self.log.debug("%s plugin didn't run, signing intent will not be adjusted",
                           PLUGIN_KOJI_PARENT_KEY)
            return

        build_info = plugin_result[BASE_IMAGE_KOJI_BUILD]

        try:
            parent_signing_intent_name = build_info['extra']['image']['odcs']['signing_intent']
        except (KeyError, TypeError):
            self.log.debug('Parent koji build, %s(%s), does not define signing_intent. '
                           'Cannot adjust for current build.',
                           build_info['nvr'], build_info['id'])
            return

        self._parent_signing_intent = (self.odcs_config
                                       .get_signing_intent_by_name(parent_signing_intent_name))

        current_signing_intent = self.compose_config.signing_intent

        # Calculate the least restrictive signing intent
        new_signing_intent = min(self._parent_signing_intent, current_signing_intent,
                                 key=lambda x: x['restrictiveness'])

        if new_signing_intent != current_signing_intent:
            self.log.info('Signing intent downgraded to "%s" to match Koji parent build',
                          new_signing_intent['name'])
            self.compose_config.set_signing_intent(new_signing_intent['name'])

    def request_compose_if_needed(self):
        if self.compose_ids:
            self.log.debug('ODCS compose not requested, using given compose IDs')
            return

        self.compose_config.validate_for_request()

        self.compose_ids = []
        for compose_request in self.compose_config.render_requests():
            compose_info = self.odcs_client.start_compose(**compose_request)
            self.compose_ids.append(compose_info['id'])

    def wait_for_composes(self):
        self.log.debug('Waiting for ODCS composes to be available: %s', self.compose_ids)
        self.composes_info = []
        for compose_id in self.compose_ids:
            compose_info = self.odcs_client.wait_for_compose(compose_id)

            if self._needs_renewal(compose_info):
                compose_info = self.odcs_client.renew_compose(compose_id)
                compose_id = compose_info['id']
                compose_info = self.odcs_client.wait_for_compose(compose_id)

            self.composes_info.append(compose_info)

        self.compose_ids = [item['id'] for item in self.composes_info]

    def _needs_renewal(self, compose_info):
        if compose_info['state_name'] == 'removed':
            return True

        time_to_expire = datetime.strptime(compose_info['time_to_expire'],
                                           ODCS_DATETIME_FORMAT)
        now = datetime.utcnow()
        seconds_left = (time_to_expire - now).total_seconds()
        return seconds_left <= self.minimum_time_to_expire

    def resolve_signing_intent(self):
        """Determine the correct signing intent

        Regardless of what was requested, or provided as signing_intent plugin parameter,
        consult sigkeys of the actual composes used to guarantee information accuracy.
        """

        all_signing_intents = [
            self.odcs_config.get_signing_intent_by_keys(compose_info.get('sigkeys', []))
            for compose_info in self.composes_info
        ]

        # Because composes_info may contain composes that were passed as
        # plugin parameters, add the parent signing intent to avoid the
        # overall signing intent from surpassing parent's.
        if self._parent_signing_intent:
            all_signing_intents.append(self._parent_signing_intent)

        # Calculate the least restrictive signing intent
        signing_intent = min(all_signing_intents, key=lambda x: x['restrictiveness'])

        self.log.info('Signing intent for build is %s', signing_intent['name'])
        self.compose_config.set_signing_intent(signing_intent['name'])

    def forward_composes(self):
        repos_by_arch = defaultdict(list)
        # set overrides by arch if arches are available
        for compose_info in self.composes_info:
            result_repofile = compose_info['result_repofile']
            try:
                arches = compose_info['arches']
            except KeyError:
                repos_by_arch[None].append(result_repofile)
            else:
                for arch in arches.split():
                    repos_by_arch[arch].append(result_repofile)

        # we should almost never have a None entry, but if we do, we need to merge
        # it with all other repos.
        try:
            noarch_repos = repos_by_arch.pop(None)
        except KeyError:
            pass
        else:
            for repos in repos_by_arch.values():
                repos.extend(noarch_repos)
        for arch, repofiles in repos_by_arch.items():
            override_build_kwarg(self.workflow, 'yum_repourls', repofiles, arch)
        # Only set the None override if there are no other repos
        if not repos_by_arch:
            override_build_kwarg(self.workflow, 'yum_repourls', noarch_repos, None)

    def make_result(self):
        result = {
            'composes': self.composes_info,
            'signing_intent': self.compose_config.signing_intent['name'],
            'signing_intent_overridden': self.compose_config.has_signing_intent_changed(),
        }
        self.log.debug('plugin result: %s', result)
        return result

    @property
    def odcs_client(self):
        if not self._odcs_client:
            self._odcs_client = get_odcs_session(self.workflow, self.odcs_fallback)

        return self._odcs_client

    @property
    def koji_session(self):
        if not self._koji_session:
            self._koji_session = get_koji_session(self.workflow, self.koji_fallback)
        return self._koji_session


class ComposeConfig(object):

    def __init__(self, data, pulp_data, odcs_config, koji_tag=None, arches=None):
        data = data or {}
        self.packages = data.get('packages', [])
        self.modules = data.get('modules', [])
        self.pulp = {}
        if data.get('pulp_repos'):
            self.pulp = pulp_data or {}
            self.flags = None
            if data.get(UNPUBLISHED_REPOS):
                self.flags = [UNPUBLISHED_REPOS]
        self.koji_tag = koji_tag
        self.odcs_config = odcs_config
        self.arches = arches

        signing_intent_name = data.get('signing_intent', self.odcs_config.default_signing_intent)
        self.set_signing_intent(signing_intent_name)
        self._original_signing_intent_name = signing_intent_name

    def set_signing_intent(self, name):
        self.signing_intent = self.odcs_config.get_signing_intent_by_name(name)

    def has_signing_intent_changed(self):
        return self.signing_intent['name'] != self._original_signing_intent_name

    def render_requests(self):
        self.validate_for_request()

        requests = []
        if self.packages:
            requests.append(self.render_packages_request())
        elif self.modules:
            requests.append(self.render_modules_request())

        for arch in self.pulp:
            requests.append(self.render_pulp_request(arch))

        return requests

    def render_packages_request(self):
        request = {
            'source_type': 'tag',
            'source': self.koji_tag,
            'packages': self.packages,
            'sigkeys': self.signing_intent['keys'],
        }
        if self.arches:
            request['arches'] = self.arches
        return request

    def render_modules_request(self):
        return {
            'source_type': 'module',
            'source': ' '.join(self.modules),
            'sigkeys': self.signing_intent['keys'],
        }

    def render_pulp_request(self, arch):
        return {
            'source_type': 'pulp',
            'source': ' '.join(self.pulp.get(arch, [])),
            'sigkeys': [],
            'flags': self.flags,
            'arches': [arch]
        }

    def validate_for_request(self):
        """Verify enough information is available for requesting compose."""
        if not self.packages and not self.modules and not self.pulp:
            raise ValueError("Nothing to compose (no packages, modules, or enabled pulp repos)")

        if self.packages and self.modules:
            raise ValueError('Compose config cannot contain both packages and modules')

        if self.packages and not self.koji_tag:
            raise ValueError('koji_tag is required when packages are used')


class SkipResolveComposesPlugin(Exception):
    pass
