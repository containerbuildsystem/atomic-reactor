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
from atomic_reactor.util import get_build_json


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
        build_json = get_build_json()
        git_url = os.environ['SOURCE_URI']
        git_ref = os.environ.get('SOURCE_REF', None)
        image = os.environ['OUTPUT_IMAGE']
        self.target_registry = os.environ.get('OUTPUT_REGISTRY', None)

        try:
            self.plugins_json = os.environ['ATOMIC_REACTOR_PLUGINS']
        except KeyError:
            try:
                self.plugins_json = os.environ['DOCK_PLUGINS']
            except KeyError:
                raise RuntimeError("No plugin configuration found!")
            else:
                self.log.warning("DOCK_PLUGINS is deprecated - please update your osbs-client!")

        self.plugins_json = json.loads(self.plugins_json)

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

    @classmethod
    def is_autousable(cls):
        return 'BUILD' in os.environ and 'SOURCE_URI' in os.environ and 'OUTPUT_IMAGE' in os.environ
