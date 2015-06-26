"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Reads input from environment variable
"""
import json
import os
from atomic_reactor.constants import BUILD_JSON_ENV

from atomic_reactor.plugin import InputPlugin


class EnvInputPlugin(InputPlugin):
    key = "env"

    def __init__(self, env_name=None, **kwargs):
        """
        constructor
        """
        # call parent constructor
        super(EnvInputPlugin, self).__init__(**kwargs)
        self.env_name = env_name

    def run(self):
        """
        get json with build config from environment variable
        """
        env_name = self.env_name or BUILD_JSON_ENV
        try:
            build_cfg_json = os.environ[env_name]
        except KeyError:
            self.log.error("build config not found in env variable '%s'", env_name)
            return None
        else:
            try:
                return self.substitute_configuration(json.loads(build_cfg_json))
            except ValueError:
                self.log.error("couldn't load build config: invalid json")
                return None

    @classmethod
    def is_autousable(cls):
        return BUILD_JSON_ENV in os.environ
