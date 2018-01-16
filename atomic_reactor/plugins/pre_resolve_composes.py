"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals

from atomic_reactor.constants import (PLUGIN_KOJI_PARENT_KEY, PLUGIN_RESOLVE_COMPOSES_KEY,
                                      REPO_CONTAINER_CONFIG)

from atomic_reactor.odcs_util import ODCSClient
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.build_orchestrate_build import override_build_kwarg
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild
from atomic_reactor.plugins.pre_reactor_config import get_config
from datetime import datetime, timedelta

try:
    from atomic_reactor.koji_util import create_koji_session
except ImportError:
    # koji module is only required in some cases.
    def create_koji_session(*args, **kwargs):
        raise RuntimeError('Missing koji module')

import os
import yaml


ODCS_DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
MINIMUM_TIME_TO_EXPIRE = timedelta(hours=2).total_seconds()


class ResolveComposesPlugin(PreBuildPlugin):
    """Request a new, or use existing, ODCS compose

    This plugin will read the configuration in git repository
    and request ODCS to create a corresponding yum repository.
    """

    key = PLUGIN_RESOLVE_COMPOSES_KEY
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow,
                 odcs_url,
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

        if koji_target and not koji_hub:
            raise ValueError('koji_hub is required when koji_target is used')

        self.signing_intent = signing_intent
        self.compose_ids = compose_ids
        self.odcs_url = odcs_url
        self.odcs_insecure = odcs_insecure
        self.odcs_openidc_secret_path = odcs_openidc_secret_path
        self.odcs_ssl_secret_path = odcs_ssl_secret_path
        self.koji_target = koji_target
        self.koji_hub = koji_hub
        self.koji_ssl_certs_dir = koji_ssl_certs_dir
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

    def read_configs(self):
        self.odcs_config = get_config(self.workflow).get_odcs_config()
        if not self.odcs_config:
            raise SkipResolveComposesPlugin('ODCS config not found')

        workdir = self.workflow.source.get_build_file_path()[1]
        file_path = os.path.join(workdir, REPO_CONTAINER_CONFIG)
        data = None
        if os.path.exists(file_path):
            with open(file_path) as f:
                data = (yaml.load(f) or {}).get('compose')

        if not data and not self.compose_ids:
            raise SkipResolveComposesPlugin('"compose" config not set and compose_ids not given')

        self.compose_config = ComposeConfig(data, self.odcs_config)

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

        build_info = plugin_result['parent-image-koji-build']

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

        # Calculate the least restrictive signinig intent
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

        compose_request = self.compose_config.render_request()
        compose_info = self.odcs_client.start_compose(**compose_request)
        self.compose_ids = [compose_info['id'], ]

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

        # Calculate the least restrictive signinig intent
        signing_intent = min(all_signing_intents, key=lambda x: x['restrictiveness'])

        self.log.info('Signing intent for build is %s', signing_intent['name'])
        self.compose_config.set_signing_intent(signing_intent['name'])

    def forward_composes(self):
        yum_repourls = [compose_info['result_repofile'] for compose_info in self.composes_info]
        override_build_kwarg(self.workflow, 'yum_repourls', yum_repourls)

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
            client_kwargs = {'insecure': self.odcs_insecure}
            if self.odcs_openidc_secret_path:
                token_path = os.path.join(self.odcs_openidc_secret_path, 'token')
                with open(token_path, "r") as f:
                    client_kwargs['token'] = f.read().strip()

            if self.odcs_ssl_secret_path:
                cert_path = os.path.join(self.odcs_ssl_secret_path, 'cert')
                if os.path.exists(cert_path):
                    client_kwargs['cert'] = cert_path

            self._odcs_client = ODCSClient(self.odcs_url, **client_kwargs)

        return self._odcs_client

    @property
    def koji_session(self):
        if not self._koji_session:
            koji_auth_info = None
            if self.koji_ssl_certs_dir:
                koji_auth_info = {
                    'ssl_certs_dir': self.koji_ssl_certs_dir,
                }
            self._koji_session = create_koji_session(self.koji_hub, koji_auth_info)

        return self._koji_session


class ComposeConfig(object):

    def __init__(self, data, odcs_config, koji_tag=None):
        data = data or {}
        self.packages = data.get('packages', [])
        self.modules = data.get('modules', [])
        self.koji_tag = koji_tag
        self.odcs_config = odcs_config

        signing_intent_name = data.get('signing_intent', self.odcs_config.default_signing_intent)
        self.set_signing_intent(signing_intent_name)
        self._original_signing_intent_name = signing_intent_name

    def set_signing_intent(self, name):
        self.signing_intent = self.odcs_config.get_signing_intent_by_name(name)

    def has_signing_intent_changed(self):
        return self.signing_intent['name'] != self._original_signing_intent_name

    def render_request(self):
        self.validate_for_request()

        request = None
        if self.packages:
            request = self.render_packages_request()
        else:
            request = self.render_modules_request()

        return request

    def render_packages_request(self):
        return {
            'source_type': 'tag',
            'source': self.koji_tag,
            'packages': self.packages,
            'sigkeys': self.signing_intent['keys']
        }

    def render_modules_request(self):
        return {
            'source_type': 'module',
            'source': ' '.join(self.modules),
            'sigkeys': self.signing_intent['keys']
        }

    def validate_for_request(self):
        """Verify enough information is available for requesting compose."""
        if not self.packages and not self.modules:
            raise ValueError('List of packages or modules cannot be empty')

        if self.packages and self.modules:
            raise ValueError('Compose config cannot contain both packages and modules')

        if self.packages and not self.koji_tag:
            raise ValueError('koji_tag is required when packages are used')


class SkipResolveComposesPlugin(Exception):
    pass
