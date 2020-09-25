"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals, absolute_import

from copy import deepcopy
from datetime import datetime, timedelta
import os
from collections import defaultdict

from atomic_reactor.utils.odcs import WaitComposeToFinishTimeout
from osbs.repo_utils import ModuleSpec

from atomic_reactor.constants import (PLUGIN_KOJI_PARENT_KEY, PLUGIN_RESOLVE_COMPOSES_KEY,
                                      REPO_CONTENT_SETS_CONFIG, BASE_IMAGE_KOJI_BUILD)

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.build_orchestrate_build import override_build_kwarg
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild
from atomic_reactor.plugins.pre_reactor_config import (get_config,
                                                       get_odcs_session,
                                                       get_koji_session, get_koji)
from atomic_reactor.util import (get_platforms,
                                 is_isolated_build,
                                 is_scratch_build,
                                 read_yaml_from_file_path)

ODCS_DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
MINIMUM_TIME_TO_EXPIRE = timedelta(hours=2).total_seconds()
# flag to let ODCS see hidden pulp repos
UNPUBLISHED_REPOS = 'include_unpublished_pulp_repos'
# flag to let ODCS ignore missing content sets
IGNORE_ABSENT_REPOS = 'ignore_absent_pulp_repos'


class ResolveComposesPlugin(PreBuildPlugin):
    """Request a new, or use existing, ODCS compose

    This plugin will read the configuration in git repository
    and request ODCS to create a corresponding yum repository.
    """

    key = PLUGIN_RESOLVE_COMPOSES_KEY
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow,
                 koji_target=None,
                 signing_intent=None,
                 compose_ids=tuple(),
                 repourls=None,
                 minimum_time_to_expire=MINIMUM_TIME_TO_EXPIRE,
                 ):
        """
        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param koji_target: str, koji target contains build tag to be used
                            when requesting compose from "tag"
        :param signing_intent: override the signing intent from git repo configuration
        :param compose_ids: use the given compose_ids instead of requesting a new one
        :param repourls: list of str, URLs to the repo files
        :param minimum_time_to_expire: int, used in deciding when to extend compose's time
                                       to expire in seconds
        """
        super(ResolveComposesPlugin, self).__init__(tasker, workflow)

        if signing_intent and compose_ids:
            raise ValueError('signing_intent and compose_ids cannot be used at the same time')

        self.signing_intent = signing_intent
        self.compose_ids = compose_ids

        self.koji_target = koji_target
        if koji_target:
            if not get_koji(self.workflow)['hub_url']:
                raise ValueError('koji_hub is required when koji_target is used')

        self.minimum_time_to_expire = minimum_time_to_expire

        self._koji_session = None
        self._odcs_client = None
        self.odcs_config = None
        self.compose_config = None
        self.composes_info = None
        self._parent_signing_intent = None
        self.repourls = repourls or []
        self.has_complete_repos = len(self.repourls) > 0
        self.plugin_result = self.workflow.prebuild_results.get(PLUGIN_KOJI_PARENT_KEY)
        self.all_compose_ids = list(self.compose_ids)

    def run(self):
        try:
            self.adjust_for_autorebuild()
            if self.allow_inheritance():
                self.adjust_for_inherit()
            self.workflow.all_yum_repourls = self.repourls
            self.read_configs()
            self.adjust_compose_config()
            self.request_compose_if_needed()
            try:
                self.wait_for_composes()
            except WaitComposeToFinishTimeout as e:
                self.log.info(str(e))
                self.log.info('Canceling the compose %s', e.compose_id)
                self.odcs_client.cancel_compose(e.compose_id)
                raise
            self.resolve_signing_intent()
            self.forward_composes()
            return self.make_result()
        except SkipResolveComposesPlugin as abort_exc:
            override_build_kwarg(self.workflow, 'yum_repourls', self.repourls, None)
            self.log.info('Aborting plugin execution: %s', abort_exc)

    def allow_inheritance(self):
        """Returns boolean if composes can be inherited"""
        if not self.workflow.source.config.inherit:
            return False
        self.log.info("Inheritance requested in config file")

        if is_scratch_build(self.workflow) or is_isolated_build(self.workflow):
            self.log.warning(
                "Inheritance is not allowed for scratch or isolated builds. "
                "Skipping inheritance.")
            return False

        return True

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
            self.all_compose_ids = []

    def adjust_for_inherit(self):
        if self.workflow.builder.dockerfile_images.base_from_scratch:
            self.log.debug('This is a base image based on scratch. '
                           'Skipping adjusting composes for inheritance.')
            return

        if not self.plugin_result:
            return

        build_info = self.plugin_result[BASE_IMAGE_KOJI_BUILD]
        parent_compose_ids = []
        parent_repourls = []

        try:
            parent_compose_ids = build_info['extra']['image']['odcs']['compose_ids']
        except (KeyError, TypeError):
            self.log.debug('Parent koji build, %s(%s), does not define compose_ids.'
                           'Cannot add compose_ids for inheritance from parent.',
                           build_info['nvr'], build_info['id'])
        try:
            parent_repourls = build_info['extra']['image']['yum_repourls']
        except (KeyError, TypeError):
            self.log.debug('Parent koji build, %s(%s), does not define yum_repourls. '
                           'Cannot add yum_repourls for inheritance from parent.',
                           build_info['nvr'], build_info['id'])

        all_compose_ids = set(self.compose_ids)
        original_compose_ids = deepcopy(all_compose_ids)
        all_compose_ids.update(parent_compose_ids)
        self.all_compose_ids = list(all_compose_ids)
        for compose_id in all_compose_ids:
            if compose_id not in original_compose_ids:
                self.log.info('Inheriting compose id %s', compose_id)

        all_yum_repos = set(self.repourls)
        original_yum_repos = deepcopy(all_yum_repos)
        all_yum_repos.update(parent_repourls)
        self.repourls = list(all_yum_repos)
        for repo in all_yum_repos:
            if repo not in original_yum_repos:
                self.log.info('Inheriting yum repo %s', repo)
        if len(parent_repourls) > 0:
            self.has_complete_repos = True

    def read_configs(self):
        self.odcs_config = get_config(self.workflow).get_odcs_config()
        if not self.odcs_config:
            raise SkipResolveComposesPlugin('ODCS config not found')

        data = self.workflow.source.config.compose
        if not data and not self.all_compose_ids:
            raise SkipResolveComposesPlugin('"compose" config not set and compose_ids not given')

        workdir = self.workflow.source.get_build_file_path()[1]
        file_path = os.path.join(workdir, REPO_CONTENT_SETS_CONFIG)
        pulp_data = None
        if os.path.exists(file_path):
            pulp_data = read_yaml_from_file_path(file_path, 'schemas/content_sets.json') or {}

        platforms = get_platforms(self.workflow)
        if platforms:
            platforms = sorted(platforms)  # sorted to keep predictable for tests

        koji_tag = None
        if self.koji_target:
            target_info = self.koji_session.getBuildTarget(self.koji_target, strict=True)
            koji_tag = target_info['build_tag_name']

        self.compose_config = ComposeConfig(data, pulp_data, self.odcs_config, koji_tag=koji_tag,
                                            arches=platforms)

    def adjust_compose_config(self):
        if self.signing_intent:
            self.compose_config.set_signing_intent(self.signing_intent)

        self.adjust_signing_intent_from_parent()

    def adjust_signing_intent_from_parent(self):
        if self.workflow.builder.dockerfile_images.base_from_scratch:
            self.log.debug('This is a base image based on scratch. '
                           'Signing intent will not be adjusted for it.')
            return

        if not self.plugin_result:
            self.log.debug("%s plugin didn't run, signing intent will not be adjusted",
                           PLUGIN_KOJI_PARENT_KEY)
            return

        build_info = self.plugin_result[BASE_IMAGE_KOJI_BUILD]

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

        if not self.workflow.source.config.compose:
            self.log.debug('ODCS compose not provided, using parents compose IDs')
            return

        self.compose_config.validate_for_request()

        for compose_request in self.compose_config.render_requests():
            compose_info = self.odcs_client.start_compose(**compose_request)
            self.all_compose_ids.append(compose_info['id'])

    def wait_for_composes(self):
        self.log.debug('Waiting for ODCS composes to be available: %s', self.all_compose_ids)
        self.composes_info = []
        for compose_id in self.all_compose_ids:
            compose_info = self.odcs_client.wait_for_compose(compose_id)

            if self._needs_renewal(compose_info):
                sigkeys = compose_info.get('sigkeys', '').split()
                updated_signing_intent = self.odcs_config.get_signing_intent_by_keys(sigkeys)
                if set(sigkeys) != set(updated_signing_intent['keys']):
                    self.log.info('Updating signing keys in "%s" from "%s", to "%s" in compose '
                                  '"%s" due to sigkeys deprecation',
                                  updated_signing_intent['name'],
                                  sigkeys,
                                  updated_signing_intent['keys'],
                                  compose_info['id']
                                  )
                    sigkeys = updated_signing_intent['keys']

                compose_info = self.odcs_client.renew_compose(compose_id, sigkeys)
                compose_id = compose_info['id']
                compose_info = self.odcs_client.wait_for_compose(compose_id)

            self.composes_info.append(compose_info)

            # A module compose is not standalone - it depends on packages from the
            # virtual platform module - if no extra repourls or other composes are
            # provided, we'll need packages from the target build tag using the
            # 'koji' plugin.

            # We assume other types of composes might provide all the packages needed -
            # though we don't really know that for sure - a compose with packages
            # listed might list all the packages that are needed, or might also require
            # packages from some other source.

            if compose_info['source_type'] != 2:  # PungiSourceType.MODULE
                self.has_complete_repos = True

        self.all_compose_ids = [item['id'] for item in self.composes_info]

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

        # we should almost never have a None entry from composes,
        # but we can have yum_repos added, so if we do, we need to merge
        # it with all other repos.
        if self.repourls:
            repos_by_arch[None].extend(self.repourls)
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

        # If we don't think the set of packages available from the user-supplied repourls,
        # inherited repourls, and composed repositories is complete, set the 'include_koji_repo'
        # kwarg so that the so that the 'yum_repourls' kwarg that we just set doesn't
        # result in the 'koji' plugin being omitted.
        if not self.has_complete_repos:
            override_build_kwarg(self.workflow, 'include_koji_repo', True)

        # So that plugins like flatpak_update_dockerfile can get information about the composes
        override_build_kwarg(self.workflow, 'compose_ids', self.all_compose_ids)

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
            self._odcs_client = get_odcs_session(self.workflow)

        return self._odcs_client

    @property
    def koji_session(self):
        if not self._koji_session:
            self._koji_session = get_koji_session(self.workflow)
        return self._koji_session


class ComposeConfig(object):

    def __init__(self, data, pulp_data, odcs_config, koji_tag=None, arches=None):
        data = data or {}
        self.use_packages = 'packages' in data
        self.packages = data.get('packages', [])
        self.modules = data.get('modules', [])
        self.pulp = {}
        self.arches = arches or []
        self.multilib_arches = []
        self.multilib_method = None
        self.modular_tags = data.get('modular_koji_tags')
        self.koji_tag = koji_tag

        if self.modular_tags is True:
            if not self.koji_tag:
                raise ValueError('koji_tag is required when modular_koji_tags is True')
            self.modular_tags = [self.koji_tag]

        if data.get('pulp_repos'):
            for arch in pulp_data or {}:
                if arch in self.arches:
                    self.pulp[arch] = pulp_data[arch]
            self.flags = []
            if data.get(UNPUBLISHED_REPOS):
                self.flags.append(UNPUBLISHED_REPOS)
            if data.get(IGNORE_ABSENT_REPOS):
                self.flags.append(IGNORE_ABSENT_REPOS)

            build_only_content_sets = data.get('build_only_content_sets', {})
            if build_only_content_sets:
                for arch, cont_sets in build_only_content_sets.items():
                    self.pulp[arch] = set(cont_sets).union(self.pulp.get(arch, []))

        for arch in data.get('multilib_arches', []):
            if arch in arches:
                self.multilib_arches.append(arch)
        if self.multilib_arches:
            self.multilib_method = data.get('multilib_method')

        self.odcs_config = odcs_config

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
        if self.use_packages:
            requests.append(self.render_packages_request())
        if self.modules:
            requests.append(self.render_modules_request())
        if self.modular_tags:
            requests.append(self.render_modular_tags_request())

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
        if self.multilib_arches:
            request['multilib_arches'] = self.multilib_arches
            request['multilib_method'] = self.multilib_method
        return request

    def render_modular_tags_request(self):
        request = {
            'source_type': 'tag',
            'source': self.koji_tag,
            'sigkeys': self.signing_intent['keys'],
            'modular_koji_tags': self.modular_tags
        }
        if self.arches:
            request['arches'] = self.arches
        if self.multilib_arches:
            request['multilib_arches'] = self.multilib_arches
            request['multilib_method'] = self.multilib_method
        return request

    def render_modules_request(self):
        # In the Flatpak case, the profile is used to determine which packages
        # are installed into the Flatpak. But ODCS doesn't understand profiles,
        # and they won't affect the compose in any case.
        noprofile_modules = [ModuleSpec.from_str(m).to_str(include_profile=False)
                             for m in self.modules]
        request = {
            'source_type': 'module',
            'source': ' '.join(noprofile_modules),
            'sigkeys': self.signing_intent['keys'],
        }
        if self.arches:
            request['arches'] = self.arches
        return request

    def render_pulp_request(self, arch):
        request = {
            'source_type': 'pulp',
            'source': ' '.join(self.pulp.get(arch, [])),
            'sigkeys': [],
            'flags': self.flags,
            'arches': [arch]
        }
        if arch in self.multilib_arches:
            request['multilib_arches'] = [arch]
            request['multilib_method'] = self.multilib_method
        return request

    def validate_for_request(self):
        """Verify enough information is available for requesting compose."""
        if not self.use_packages and not self.modules and not self.pulp and not self.modular_tags:
            raise ValueError("Nothing to compose (no packages, modules, modular_tags "
                             "or enabled pulp repos)")

        if self.packages and not self.koji_tag:
            raise ValueError('koji_tag is required when packages are used')


class SkipResolveComposesPlugin(Exception):
    pass
