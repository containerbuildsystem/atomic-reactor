"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import os
import logging

try:
    # py3
    from configparser import SafeConfigParser
except ImportError:
    # py2
    from ConfigParser import SafeConfigParser

from dock.core import DockerTasker
from dock.plugins.post_push_to_pulp import push_image_to_pulp

import pytest


PULP_CONF_PATH = os.path.expanduser("~/.pulp/admin.conf")


@pytest.mark.skipif(not os.path.exists(PULP_CONF_PATH),
                    reason="no pulp config found at %s" % PULP_CONF_PATH)
def test_pulp():
    tasker = DockerTasker()
    parsed_config = SafeConfigParser()
    assert len(parsed_config.read(PULP_CONF_PATH)) > 0

    host = parsed_config.get("server", "host")
    un = parsed_config.get("server", "username")
    pswd = parsed_config.get("server", "password")
    verify_ssl = parsed_config.getboolean("server", "verify_ssl")
    push_image_to_pulp("busybox-test", "busybox", host, un, pswd, verify_ssl,
                       tasker, logging.getLogger("dock.tests"))
