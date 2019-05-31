"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Reads input from OpenShift v3
"""
from __future__ import absolute_import

import json
import os

from atomic_reactor.plugin import InputPlugin
from atomic_reactor.util import get_build_json, read_yaml
from osbs.utils import RegistryURI
from atomic_reactor.constants import (PLUGIN_BUMP_RELEASE_KEY,
                                      PLUGIN_DELETE_FROM_REG_KEY,
                                      PLUGIN_DISTGIT_FETCH_KEY,
                                      PLUGIN_DOCKERFILE_CONTENT_KEY,
                                      PLUGIN_FETCH_MAVEN_KEY,
                                      PLUGIN_INJECT_PARENT_IMAGE_KEY,
                                      PLUGIN_KOJI_IMPORT_PLUGIN_KEY,
                                      PLUGIN_KOJI_PARENT_KEY,
                                      PLUGIN_KOJI_PROMOTE_PLUGIN_KEY,
                                      PLUGIN_KOJI_TAG_BUILD_KEY,
                                      PLUGIN_KOJI_UPLOAD_PLUGIN_KEY,
                                      PLUGIN_PULP_PUBLISH_KEY,
                                      PLUGIN_PULP_PULL_KEY,
                                      PLUGIN_PULP_PUSH_KEY,
                                      PLUGIN_PULP_SYNC_KEY,
                                      PLUGIN_PULP_TAG_KEY,
                                      PLUGIN_RESOLVE_COMPOSES_KEY,
                                      PLUGIN_SENDMAIL_KEY)


class OSv3InputPlugin(InputPlugin):
    key = "osv3"

    def __init__(self, **kwargs):
        """
        constructor
        """
        # call parent constructor
        super(OSv3InputPlugin, self).__init__(**kwargs)
        self.target_registry = None
        self.reactor_env = None
        self.plugins_json = None

    def validate_user_data(self, user_params):
        # make sure the input json is valid
        read_yaml(user_params, 'schemas/user_params.json')
        return json.loads(user_params)

    def get_plugins_with_user_data(self, user_params, user_data):
        #  get the reactor config map and derive an osbs instance from it

        from osbs.api import OSBS
        from osbs.conf import Configuration

        reactor_config_override = user_data.get('reactor_config_override')
        if reactor_config_override:
            read_yaml(json.dumps(reactor_config_override), 'schemas/config.json')

        osbs_conf = Configuration(build_json_dir=user_data.get('build_json_dir'))
        osbs = OSBS(osbs_conf, osbs_conf)
        return osbs.render_plugins_configuration(user_params)

    def get_value(self, name, default=None):
        return self.reactor_env.get(name, default)

    def find_plugin(self, phase, target_plugin):
        if phase in self.plugins_json:
            for index, plugin in enumerate(self.plugins_json[phase]):
                if plugin['name'] == target_plugin:
                    return index
        return -1

    def remove_plugin(self, phase, target_plugin, reason):
        index = self.find_plugin(phase, target_plugin)
        if index >= 0:
            self.log.info('%s: removing %s from phase %s', reason, target_plugin, phase)
            del self.plugins_json[phase][index]

    def remove_koji_plugins(self):
        koji_map = self.get_value('koji', {})
        if not koji_map.get('hub_url'):
            # bump_release is removed in PluginsConfiguration if no release value
            self.remove_plugin('prebuild_plugins', PLUGIN_BUMP_RELEASE_KEY,
                               'no koji hub available')
            # inject_parent_image is removed in PluginsConfiguration if no parent image
            self.remove_plugin('prebuild_plugins', PLUGIN_INJECT_PARENT_IMAGE_KEY,
                               'no koji hub available')
            self.remove_plugin('prebuild_plugins', PLUGIN_KOJI_PARENT_KEY, 'no koji hub available')
            self.remove_plugin('postbuild_plugins', PLUGIN_KOJI_UPLOAD_PLUGIN_KEY,
                               'no koji hub available')
            self.remove_plugin('exit_plugins', PLUGIN_KOJI_PROMOTE_PLUGIN_KEY,
                               'no koji hub available')
            self.remove_plugin('exit_plugins', PLUGIN_KOJI_IMPORT_PLUGIN_KEY,
                               'no koji hub available')
            self.remove_plugin('exit_plugins', PLUGIN_KOJI_TAG_BUILD_KEY, 'no koji hub available')
            # root and hub are required, so this check is probably redundant
            if not koji_map.get('root_url'):
                self.remove_plugin('prebuild_plugins', PLUGIN_FETCH_MAVEN_KEY,
                                   'no koji root available')

    def remove_pulp_plugins(self):
        phases = ('postbuild_plugins', 'exit_plugins')
        pulp_registry = self.get_value('pulp')
        koji_hub = self.get_value('koji', {}).get('hub_url')
        for phase in phases:
            if not (pulp_registry and koji_hub):
                self.remove_plugin(phase, PLUGIN_PULP_PULL_KEY, 'no pulp or koji available')

        if not pulp_registry:
            self.remove_plugin('postbuild_plugins', PLUGIN_PULP_PUSH_KEY, 'no pulp available')
            self.remove_plugin('postbuild_plugins', PLUGIN_PULP_SYNC_KEY, 'no pulp available')
            self.remove_plugin('postbuild_plugins', PLUGIN_PULP_TAG_KEY, 'no pulp available')
            self.remove_plugin('exit_plugins', PLUGIN_DELETE_FROM_REG_KEY, 'no pulp available')
            self.remove_plugin('exit_plugins', PLUGIN_PULP_PUBLISH_KEY, 'no pulp available')
        else:
            docker_registry = None
            all_registries = self.get_value('registries', {})

            versions = self.get_value('content_versions', ['v2'])

            for registry in all_registries:
                reguri = RegistryURI(registry.get('url'))
                if reguri.version == 'v2':
                    # First specified v2 registry is the one we'll tell pulp
                    # to sync from. Keep the http prefix -- pulp wants it.
                    docker_registry = registry
                    break

            if 'v1' not in versions:
                self.remove_plugin('postbuild_plugins', PLUGIN_PULP_PUSH_KEY,
                                   'v1 content not enabled')

            if docker_registry:
                source_registry_str = self.get_value('source_registry', {}).get('url')
                perform_delete = (source_registry_str is None or
                                  RegistryURI(source_registry_str).uri != reguri.uri)
                if not perform_delete:
                    self.remove_plugin('exit_plugins', PLUGIN_DELETE_FROM_REG_KEY,
                                       'no delete needed')
            else:
                self.remove_plugin('postbuild_plugins', PLUGIN_PULP_SYNC_KEY,
                                   'no V2 pulp available')
                self.remove_plugin('exit_plugins', PLUGIN_DELETE_FROM_REG_KEY,
                                   'no V2 pulp available')

    def remove_plugins_without_parameters(self):
        """
        This used to be handled in BuildRequest, but with REACTOR_CONFIG, osbs-client doesn't have
        enough information.
        """

        # Compatibility code for dockerfile_content plugin
        self.remove_plugin('prebuild_plugins', PLUGIN_DOCKERFILE_CONTENT_KEY,
                           'dockerfile_content is deprecated, please remove from config')
        if not self.reactor_env:
            return
        self.remove_koji_plugins()
        self.remove_pulp_plugins()
        if not self.get_value('odcs'):
            self.remove_plugin('prebuild_plugins', PLUGIN_RESOLVE_COMPOSES_KEY,
                               'no odcs available')
        if not self.get_value('smtp'):
            self.remove_plugin('exit_plugins', PLUGIN_SENDMAIL_KEY, 'no mailhost available')
        if not self.get_value('sources_command'):
            self.remove_plugin('prebuild_plugins', PLUGIN_DISTGIT_FETCH_KEY, 'no sources command')

    def run(self):
        """
        each plugin has to implement this method -- it is used to run the plugin actually

        response from plugin is kept and used in json result response
        """
        build_json = get_build_json()
        git_url = os.environ['SOURCE_URI']
        git_ref = os.environ.get('SOURCE_REF', None)
        image = os.environ['OUTPUT_IMAGE']
        self.target_registry = os.environ.get('OUTPUT_REGISTRY', None)

        git_commit_depth = None
        git_branch = None
        try:
            user_params = os.environ['USER_PARAMS']
            user_data = self.validate_user_data(user_params)
            git_commit_depth = user_data.get('git_commit_depth', None)
            git_branch = user_data.get('git_branch', None)
            self.plugins_json = self.get_plugins_with_user_data(user_params, user_data)
            # if we get the USER_PARAMS, we'd better get the REACTOR_CONFIG too
            reactor_config_map = os.environ['REACTOR_CONFIG']
            self.reactor_env = read_yaml(reactor_config_map, 'schemas/config.json')
        except KeyError:
            try:
                self.plugins_json = os.environ['ATOMIC_REACTOR_PLUGINS']
            except KeyError:
                raise RuntimeError("No plugin configuration found!")

        self.plugins_json = json.loads(self.plugins_json)
        # validate json before performing any changes
        read_yaml(json.dumps(self.plugins_json), 'schemas/plugins.json')

        input_json = {
            'source': {
                'provider': 'git',
                'uri': git_url,
                'provider_params': {
                    'git_commit': git_ref,
                    'git_commit_depth': git_commit_depth,
                    'git_branch': git_branch,
                },
            },
            'image': image,
            'openshift_build_selflink': build_json.get('metadata', {}).get('selfLink', None)
        }
        input_json.update(self.plugins_json)

        self.log.debug("build json: %s", input_json)

        self.remove_plugins_without_parameters()
        # make sure the final json is valid
        read_yaml(json.dumps(self.plugins_json), 'schemas/plugins.json')

        return input_json

    @classmethod
    def is_autousable(cls):
        return 'BUILD' in os.environ and 'SOURCE_URI' in os.environ and 'OUTPUT_IMAGE' in os.environ
