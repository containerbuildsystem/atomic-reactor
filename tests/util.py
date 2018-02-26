# -*- coding: utf-8 -*-
"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import pytest
import requests
from atomic_reactor.plugins.pre_reactor_config import ODCSConfig, ClusterConfig

from six import string_types


def is_string_type(obj):
    """
    Test whether obj is a string type

    :param obj: object to test
    :return: bool, whether obj is a string type
    """

    return any(isinstance(obj, strtype)
               for strtype in string_types)


def has_connection():
    try:
        requests.get("https://github.com/")
        return True
    except requests.ConnectionError:
        return False


class mocked_reactorconfig(object):
    def __init__(self, conf):
        self.conf = conf
        self.cluster_configs = {}
        for platform, clusters in self.conf.get('clusters', {}).items():
            cluster_configs = [ClusterConfig(priority=priority, **cluster)
                               for priority, cluster in enumerate(clusters)]
            self.cluster_configs[platform] = [confi for confi in cluster_configs
                                              if confi.enabled]

    def get_odcs_config(self):
        whole_config = self.conf.get('odcs')
        odcs_config = None

        if whole_config:
            odcs_config = {}
            odcs_config['signing_intents'] = whole_config['signing_intents']
            odcs_config['default_signing_intent'] = whole_config['default_signing_intent']
            odcs_config = ODCSConfig(**odcs_config)
        return odcs_config

    def get_enabled_clusters_for_platform(self, platform):
        return self.cluster_configs.get(platform, [])

# In case we run tests in an environment without internet connection.
requires_internet = pytest.mark.skipif(not has_connection(), reason="requires internet connection")
