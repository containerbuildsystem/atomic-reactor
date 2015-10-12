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
from atomic_reactor.plugins.post_tag_and_push import TagAndPushPlugin


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
        self.target_registry = os.environ.get('OUTPUT_REGISTRY', None)
        self.plugins_json = os.environ.get('DOCK_PLUGINS', '{}')
        self.plugins_json = json.loads(self.plugins_json)

        self.preprocess_plugins()

        input_json = {
            'source': {
                'provider': 'git',
                'uri': git_url,
                'provider_params': {'git_commit': git_ref}
            },
            'image': image,
            'openshift_build_selflink': build_json.get('metadata', {}).get('selfLink', None)
        }
        input_json.update(self.plugins_json)

        self.log.debug("build json: %s", input_json)

        return input_json

    def get_plugin(self, section, name):
        try:
            plugin = [p for p in self.plugins_json[section] if p.get('name') == name][0]
        except IndexError:
            return None
        else:
            return plugin

    def preprocess_plugins(self):
        for typ in ('prebuild', 'postbuild', 'prepublish', 'exit'):
            self.plugins_json.setdefault('{0}_plugins'.format(typ), [])

        # XXX: Remove the two if blocks after we stop using super old osbs:(
        pull_plugin = self.get_plugin('prebuild_plugins', PullBaseImagePlugin.key)
        if not pull_plugin:
            self.log.warning("%s is missing in prebuild_plugins - please update your osbs-client!",
                             PullBaseImagePlugin.key)
            pull_plugin = { "name": PullBaseImagePlugin.key }
            self.plugins_json['prebuild_plugins'].insert(0, pull_plugin)

        change_plugin = self.get_plugin('prebuild_plugins', 'change_source_registry')
        if change_plugin:
            if 'registry_uri' in change_plugin.get('args', {}):
                pull_plugin.setdefault('args', {})['parent_registry'] = \
                    change_plugin['args']['registry_uri']
            if 'insecure_registry' in change_plugin.get('args', {}):
                pull_plugin.setdefault('args', {})['parent_registry_insecure'] = \
                    change_plugin['args']['insecure_registry']
            self.plugins_json['prebuild_plugins'].remove(change_plugin)

        if 'parent_registry' not in pull_plugin.get('args', {}):
            self.log.error("source registry is not configured")

        push_plugin = self.get_plugin('postbuild_plugins', TagAndPushPlugin.key)
        if not push_plugin and self.target_registry:
            self.log.warning("%s is missing in postbuild_plugins - please update your osbs-client!",
                             TagAndPushPlugin.key)
            push_plugin = { "name": TagAndPushPlugin.key }
            self.plugins_json['postbuild_plugins'].insert(0, push_plugin)

        if push_plugin and self.target_registry:
            push_plugin.setdefault('args', {}).setdefault('registries', {})
            if not push_plugin['args']['registries']:
                push_plugin['args']['registries'][self.target_registry] = {"insecure": True}

    @classmethod
    def is_autousable(cls):
        return 'BUILD' in os.environ and 'SOURCE_URI' in os.environ and 'OUTPUT_IMAGE' in os.environ
