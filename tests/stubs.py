# -*- coding: utf-8 -*-
"""
Copyright (c) 2018, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""


# Stubs for commonly-mocked classes
class StubConfig(object):

    def __init__(self):
        self.image_build_method = None
        self.release_env_var = None
        self.remote_sources_version = None
        self.remote_source = None
        self.remote_sources = None


class StubSource(object):

    def __init__(self):
        self.dockerfile_path = None
        self.path = ''
        self.config = StubConfig()

    def get_vcs_info(self):
        return None


class StubTagConf(object):
    def __init__(self):
        self.primary_images = []
        self.unique_images = []
        self.images = []

    def set_images(self, images):
        self.images = images
        return self
