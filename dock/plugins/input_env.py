"""
Reads input from environment variable
"""
import json
import os
from dock.constants import BUILD_JSON_ENV

from dock.plugin import InputPlugin


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
                self.log.error("Couldn't load build config: invalid json")
                return None
