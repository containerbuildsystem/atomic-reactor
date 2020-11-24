"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Reads input from provided path
"""
import json
import os

from atomic_reactor.constants import CONTAINER_BUILD_JSON_PATH

from atomic_reactor.plugin import InputPlugin


class PathInputPlugin(InputPlugin):
    key = "path"

    def __init__(self, path=None, **kwargs):
        """
        constructor
        """
        # call parent constructor
        super(PathInputPlugin, self).__init__(**kwargs)
        self.path = path

    def run(self):
        """
        get json with build config from path
        """
        path = self.path or CONTAINER_BUILD_JSON_PATH
        try:
            with open(path, 'r') as build_cfg_fd:
                build_cfg_json = json.load(build_cfg_fd)
        except ValueError:
            self.log.error("couldn't decode json from file '%s'", path)
            return None
        except IOError:
            self.log.error("couldn't read json from file '%s'", path)
            return None
        else:
            return self.substitute_configuration(build_cfg_json)

    @classmethod
    def is_autousable(cls):
        return os.path.exists(CONTAINER_BUILD_JSON_PATH)
