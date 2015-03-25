"""
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

        input_json = {
            'git_url': git_url,
            'image': image,
            'git_commit': git_ref,
            'target_registries': [target_registry] if target_registry is not None else None,
            'prebuild_plugins': plugins_json.get('prebuild_plugins', None),
            'postbuild_plugins': plugins_json.get('postbuild_plugins', None),
            'target_registries_insecure': True,  # FIXME: create plugin for this
        }

        self.log.debug("build json: %s", input_json)

        return input_json
