"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

log = None


def setup_logger(logger):
    raise RuntimeError


class Pulp(object):
    """
    dockpulp.Pulp stub
    """

    def __init__(self, env=None, config_file=None, config_override=None):
        pass

    def getPrefix(self):
        pass

    def login(self, username, password):
        pass

    def set_certs(self, cer, key):
        pass

    def syncRepo(self, env=None, repo=None, config_file=None, prefix_with=None,
                 feed=None, basic_auth_username=None, basic_auth_password=None,
                 ssl_validation=None):
        pass

    def crane(self, repos, wait=True):
        pass
