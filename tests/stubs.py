# -*- coding: utf-8 -*-
"""
Copyright (c) 2018, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from os.path import dirname
from osbs.utils import ImageName
from atomic_reactor.util import DockerfileImages


# Stubs for commonly-mocked classes
class StubConfig(object):
    image_build_method = None
    release_env_var = None


class StubSource(object):
    dockerfile_path = None
    path = ''
    config = StubConfig()

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
        self.dockerfile_images = DockerfileImages([])
        self.parent_images_digests = {}
        self.df_path = None
        self.df_dir = None
        self.git_dockerfile_path = None
        self.git_path = None
        self.image = None
        self.image_id = None
        self.source = StubSource()
        self.source.config = StubConfig()
        self.tasker = None
        self.original_df = None
        self.buildargs = {}

        self._inspection_data = None
        self._parent_inspection_data = {}

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

    def set_parent_inspection_data(self, image, inspection_data):
        image_name = ImageName.parse(image)
        self._parent_inspection_data[image_name] = inspection_data
        return self

    def set_source(self, source):
        self.source = source
        return self

    def set_dockerfile_images(self, parent_images):
        self.dockerfile_images = DockerfileImages(parent_images)
        return self

    # Mocked methods
    @property
    def base_image_inspect(self):
        return self._inspection_data

    def parent_image_inspect(self, image):
        image_name = ImageName.parse(image)
        return self._parent_inspection_data[image_name]
