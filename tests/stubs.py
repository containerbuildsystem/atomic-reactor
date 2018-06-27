# -*- coding: utf-8 -*-
"""
Copyright (c) 2018 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from os.path import dirname


# Stubs for commonly-mocked classes

class StubSource(object):
    dockerfile_path = None
    path = ''


class StubTagConf(object):
    def __init__(self):
        self.primary_images = []
        self.unique_images = []
        self.images = []

    def set_images(self, images):
        self.images = images
        return self


class StubInsideBuilder(object):
    """
    A test data builder for the InsideBuilder class.

    Use it like this:

    workflow = DockerBuildWorkflow(...)
    workflow.builder = (StubInsideBuilder()
                        .for_workflow(workflow)
                        .set_df_path(...)
                        .set_inspection_data({...}))
    """

    def __init__(self):
        self.base_image = None
        self.df_path = None
        self.df_dir = None
        self.git_dockerfile_path = None
        self.git_path = None
        self.image = None
        self.image_id = None
        self.source = StubSource()
        self.tag_conf = StubTagConf()

        self._inspection_data = None

    def for_workflow(self, workflow):
        return self.set_source(workflow.source).set_image(workflow.image)

    def set_df_path(self, df_path):
        self.df_path = df_path
        self.df_dir = dirname(df_path)
        return self

    def set_image(self, image):
        self.image = image
        return self

    def set_inspection_data(self, inspection_data):
        self._inspection_data = inspection_data
        return self

    def set_source(self, source):
        self.source = source
        return self

    # Mocked methods

    def inspect_base_image(self):
        return self._inspection_data
