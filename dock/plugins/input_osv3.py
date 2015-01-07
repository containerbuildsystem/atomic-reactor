"""
Reads input from OpenShift v3
"""
import json
import os

from dock.plugin import InputPlugin


class OSv3InputPlugin(InputPlugin):
    key = "osv3"

    def __init__(self):
        """
        constructor
        """
        # call parent constructor
        super(OSv3InputPlugin, self).__init__()

    def run(self):
        """
        each plugin has to implement this method -- it is used to run the plugin actually

        response from plugin is kept and used in json result response
        """
        build_json_str = os.environ['BUILD']
        build_json = json.loads(build_json_str)
        git_url = os.environ['SOURCE_URI']
        git_ref = os.environ['SOURCE_REF']
        image = os.environ['OUTPUT_IMAGE']
        target_registry = os.environ['OUTPUT_REGISTRY']

        return {
            'git_url': git_url,
            'image': image,
            'git_commit': git_ref,
            'target_registries': [target_registry]
        }
