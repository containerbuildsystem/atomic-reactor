"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Reads input from OpenShift v3
"""
import json
import os

from dock.plugin import InputPlugin


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

        source_registry = None
        source_registry_insecure = None
        try:
            match = [x for x in plugins_json['prebuild_plugins'] if x.get('name', None) == 'change_source_registry']
            source_registry = match[0]['args']['registry_uri']
            source_registry_insecure = match[0]['args'].get('insecure_registry', False)
        except (IndexError, KeyError) as ex:
            self.log.error("source registry is not configured: '%s'", repr(ex))

        input_json = {
            'git_url': git_url,
            'image': image,
            'git_commit': git_ref,
            'target_registries': [target_registry] if target_registry is not None else None,
            'prebuild_plugins': plugins_json.get('prebuild_plugins', None),
            'postbuild_plugins': plugins_json.get('postbuild_plugins', None),
            'target_registries_insecure': True,  # FIXME: create plugin for this
            'parent_registry': source_registry,
            'parent_registry_insecure': source_registry_insecure,
        }

        self.log.debug("build json: %s", input_json)

        return input_json
