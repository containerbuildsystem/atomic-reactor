"""
Copyright (c) 2015, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Reads input from OpenShift v3
"""
from __future__ import absolute_import

import json
import os

from osbs.constants import USER_PARAMS_KIND_SOURCE_CONTAINER_BUILDS

from atomic_reactor.plugin import InputPlugin
from atomic_reactor.util import get_build_json, read_yaml
from atomic_reactor.constants import (PLUGIN_BUMP_RELEASE_KEY,
                                      PLUGIN_DISTGIT_FETCH_KEY,
                                      PLUGIN_FETCH_MAVEN_KEY,
                                      PLUGIN_INJECT_PARENT_IMAGE_KEY,
                                      PLUGIN_KOJI_IMPORT_PLUGIN_KEY,
                                      PLUGIN_KOJI_PARENT_KEY,
                                      PLUGIN_KOJI_PROMOTE_PLUGIN_KEY,
                                      PLUGIN_KOJI_TAG_BUILD_KEY,
                                      PLUGIN_KOJI_UPLOAD_PLUGIN_KEY,
                                      PLUGIN_RESOLVE_COMPOSES_KEY,
                                      PLUGIN_SENDMAIL_KEY,
                                      PLUGIN_KOJI_DELEGATE_KEY)


def get_plugins_with_user_data(user_params, user_data):
    """Get the reactor config map and derive an osbs instance from it"""

    from osbs.api import OSBS
    from osbs.conf import Configuration

    reactor_config_override = user_data.get('reactor_config_override')
    if reactor_config_override:
        read_yaml(json.dumps(reactor_config_override), 'schemas/config.json')

    osbs_conf = Configuration(build_json_dir=user_data.get('build_json_dir'))
    osbs = OSBS(osbs_conf, osbs_conf)
    return osbs.render_plugins_configuration(user_params)


def validate_user_data(user_params, schema_path):
    """Validates JSON user data against schema and returns them in python dict

    :param str user_params: JSON with user data
    :param str schema_path: path to JSON schema definitions
    :return: dict with user data
    """
    read_yaml(user_params, schema_path)
    return json.loads(user_params)


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
        if not koji_map['hub_url']:
            self.remove_plugin('prebuild_plugins', PLUGIN_KOJI_DELEGATE_KEY,
                               'no koji hub available')
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
            if not koji_map['root_url']:
                self.remove_plugin('prebuild_plugins', PLUGIN_FETCH_MAVEN_KEY,
                                   'no koji root available')

    def remove_plugins_without_parameters(self):
        """
        This used to be handled in BuildRequest, but with REACTOR_CONFIG, osbs-client doesn't have
        enough information.
        """

        if not self.reactor_env:
            return
        self.remove_koji_plugins()
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
        self.target_registry = os.environ.get('OUTPUT_REGISTRY', None)

        try:
            user_params = os.environ['USER_PARAMS']
            user_data = validate_user_data(user_params, 'schemas/user_params.json')
            git_commit_depth = user_data.get('git_commit_depth', None)
            git_branch = user_data.get('git_branch', None)
            arrangement_version = user_data.get('arrangement_version', None)
            self.plugins_json = get_plugins_with_user_data(user_params, user_data)
            # if we get the USER_PARAMS, we'd better get the REACTOR_CONFIG too
            reactor_config_map = os.environ['REACTOR_CONFIG']
            self.reactor_env = read_yaml(reactor_config_map, 'schemas/config.json')
        except KeyError:
            raise RuntimeError("No plugin configuration found!")

        if arrangement_version and arrangement_version <= 5:
            raise ValueError('arrangement_version <= 5 is no longer supported')

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
        return 'BUILD' in os.environ and 'SOURCE_URI' in os.environ


class OSv3SourceContainerInputPlugin(InputPlugin):
    """Input plugin for building source container images"""
    key = "osv3_source_container"

    def __init__(self, **kwargs):
        """
        constructor
        """
        # call parent constructor
        super(OSv3SourceContainerInputPlugin, self).__init__(**kwargs)
        self.target_registry = None
        self.reactor_env = None
        self.plugins_json = None

    def assert_koji_integration(self):
        """Fail when koji integration is nto configured as koji is integral
        part of source containers feature"""
        koji_map = self.reactor_env.get('koji', {})
        if not koji_map['hub_url']:
            raise RuntimeError(
                "Koji-hub URL is not configured. Source container image "
                "builds require enabled koji integration"
            )

    def run(self):
        build_json = get_build_json()
        self.target_registry = os.environ.get('OUTPUT_REGISTRY', None)

        user_params = os.environ['USER_PARAMS']
        user_data = validate_user_data(user_params, 'schemas/source_containers_user_params.json')
        arrangement_version = user_data.get('arrangement_version', None)
        plugins_json_serialized = get_plugins_with_user_data(user_params, user_data)
        # if we get the USER_PARAMS, we'd better get the REACTOR_CONFIG too
        reactor_config_map = os.environ['REACTOR_CONFIG']
        self.reactor_env = read_yaml(reactor_config_map, 'schemas/config.json')

        if arrangement_version and arrangement_version <= 5:
            raise ValueError('arrangement_version <= 5 is no longer supported')

        # validate json before performing any changes
        read_yaml(plugins_json_serialized, 'schemas/plugins.json')
        self.plugins_json = json.loads(plugins_json_serialized)

        input_json = {
            'openshift_build_selflink': build_json.get('metadata', {}).get('selfLink', None)
        }
        input_json.update(self.plugins_json)

        self.log.debug("build json: %s", input_json)

        self.assert_koji_integration()

        # validate after performing changes
        read_yaml(json.dumps(self.plugins_json), 'schemas/plugins.json')

        return input_json

    @classmethod
    def is_autousable(cls):
        return (
            'USER_PARAMS' in os.environ and
            json.loads(os.environ['USER_PARAMS'])
                .get('kind') == USER_PARAMS_KIND_SOURCE_CONTAINER_BUILDS
        )
