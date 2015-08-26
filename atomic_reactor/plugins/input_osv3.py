"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Reads input from OpenShift v3
"""
import json
import os

from atomic_reactor.plugin import InputPlugin
from atomic_reactor.plugins.pre_pull_base_image import PullBaseImagePlugin


class OSv3InputPlugin(InputPlugin):
    key = "osv3"

    def __init__(self, **kwargs):
        """
        constructor
        """
        # call parent constructor
        super(OSv3InputPlugin, self).__init__(**kwargs)

    def run(self):
        """
        each plugin has to implement this method -- it is used to run the plugin actually

        response from plugin is kept and used in json result response
        """
        build_json_str = os.environ['BUILD']
        build_json = json.loads(build_json_str)
        git_url = os.environ['SOURCE_URI']
        git_ref = os.environ.get('SOURCE_REF', None)
        image = os.environ['OUTPUT_IMAGE']
        target_registry = os.environ.get('OUTPUT_REGISTRY', None)
        plugins_json = os.environ.get('DOCK_PLUGINS', '{}')
        plugins_json = json.loads(plugins_json)

        plugins_json.setdefault('prebuild_plugins', [])

        # XXX: Remove the two try-except blocks after Sep 2015
        try:
            pull_plugin = [p for p in plugins_json['prebuild_plugins']
                           if p.get('name', None) == PullBaseImagePlugin.key][0]
        except IndexError:
            self.log.warning("%s is missing in prebuild_plugins - please update your osbs-client!",
                             PullBaseImagePlugin.key)
            pull_plugin = { "name": PullBaseImagePlugin.key }
            plugins_json['prebuild_plugins'].insert(0, pull_plugin)

        try:
            change_plugin = [p for p in plugins_json['prebuild_plugins']
                             if p.get('name', None) == 'change_source_registry'][0]
        except IndexError:
            pass
        else:
            if 'registry_uri' in change_plugin.get('args', {}):
                pull_plugin.setdefault('args', {})['parent_registry'] = \
                    change_plugin['args']['registry_uri']
            if 'insecure_registry' in change_plugin.get('args', {}):
                pull_plugin.setdefault('args', {})['parent_registry_insecure'] = \
                    change_plugin['args']['insecure_registry']
            plugins_json['prebuild_plugins'].remove(change_plugin)

        if 'parent_registry' not in pull_plugin.get('args', {}):
            self.log.error("source registry is not configured")

        input_json = {
            'source': {
                'provider': 'git',
                'uri': git_url,
                'provider_params': {'git_commit': git_ref}
            },
            'image': image,
            'target_registries': [target_registry] if target_registry else None,
            'target_registries_insecure': True,  # FIXME: create plugin for this
            'openshift_build_selflink': build_json.get('metadata', {}).get('selfLink', None)
        }
        input_json.update(plugins_json)

        self.log.debug("build json: %s", input_json)

        return input_json

    @classmethod
    def is_autousable(cls):
        return 'BUILD' in os.environ and 'SOURCE_URI' in os.environ and 'OUTPUT_IMAGE' in os.environ
