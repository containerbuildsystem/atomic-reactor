"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import logging
import os
import pytest

from flexmock import flexmock

from atomic_reactor.constants import EXPORTED_SQUASHED_IMAGE_NAME
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PrePublishPluginsRunner, PluginFailedException
from atomic_reactor.plugins import exit_remove_built_image
from atomic_reactor.plugins.prepub_squash import PrePublishSquashPlugin
from atomic_reactor.util import ImageName
from docker_squash.squash import Squash
from tests.constants import MOCK, MOCK_SOURCE


if MOCK:
    from tests.docker_mock import mock_docker


DUMMY_TARBALL = {
    'contents': 'dummy file contents',
    'md5sum': '79cad6cda5ebe6b9bdbdbb6a56587e28',
    'sha256sum': '5fefd3ff57b97c856958bfc0333231f4f8a600b305d749c9616b0879765f2472',
    'size': 19
}


SET_DEFAULT_LAYER_ID = object()


class MockInsideBuilder(object):
    def __init__(self):
        self.tasker = DockerTasker()
        self.base_image = ImageName(repo='Fedora', tag='22')
        self.image_id = 'image_id'
        self.image = 'image'
        self.df_path = 'df_path'
        self.df_dir = 'df_dir'

    @property
    def source(self):
        result = flexmock()
        setattr(result, 'dockerfile_path', '/')
        setattr(result, 'path', '/tmp')
        return result


class TestSquashPlugin(object):

    def setup_method(self, method):
        if MOCK:
            mock_docker()
        self.workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
        self.workflow.builder = MockInsideBuilder()
        self.tasker = self.workflow.builder.tasker

        # Expected path for exported squashed image.
        self.output_path = None

    def test_default_parameters(self):
        self.should_squash_with_kwargs()
        self.run_plugin_with_args({})

    @pytest.mark.parametrize(('plugin_tag', 'squash_tag'), (
        ('spam', 'spam'),
        (None, 'image'),
        ('', 'image'),
    ))
    def test_tag_value_is_used(self, plugin_tag, squash_tag):
        self.should_squash_with_kwargs(tag=squash_tag)
        self.run_plugin_with_args({'tag': plugin_tag})

    @pytest.mark.parametrize('dont_load', (True, False))
    def test_dont_load_is_honored(self, dont_load):
        self.should_squash_with_kwargs(load_image=not dont_load)
        self.run_plugin_with_args({'dont_load': dont_load})

    @pytest.mark.parametrize(('from_base', 'from_layer', 'squash_from_layer'), (
        (False, 'from-layer', 'from-layer'),
        (True, 'from-layer', 'from-layer'),
        (False, None, None),
        (True, None, SET_DEFAULT_LAYER_ID),
    ))
    def test_from_specified(self, from_base, from_layer, squash_from_layer):
        self.should_squash_with_kwargs(from_layer=squash_from_layer)
        self.run_plugin_with_args({'from_base': from_base, 'from_layer': from_layer})

    def test_missing_base_image_id(self):
        if MOCK:
            mock_docker(inspect_should_fail=True)
        self.should_squash_with_kwargs(from_layer=None)
        with pytest.raises(PluginFailedException):
            self.run_plugin_with_args({'from_layer': None})

    def should_squash_with_kwargs(self, **kwargs):
        kwargs.setdefault('image', self.workflow.builder.image_id)
        kwargs.setdefault('load_image', True)
        kwargs.setdefault('log', logging.Logger)
        kwargs.setdefault('output_path', os.path.join(self.workflow.source.workdir,
                                                      EXPORTED_SQUASHED_IMAGE_NAME))
        kwargs.setdefault('tag', self.workflow.builder.image)

        # Avoid inspect errors at this point
        if 'from_layer' not in kwargs:
            kwargs['from_layer'] = self.workflow.base_image_inspect['Id']

        self.output_path = kwargs['output_path']

        if kwargs.get('from_layer') == SET_DEFAULT_LAYER_ID:
            kwargs['from_layer'] = self.workflow.base_image_inspect['Id']

        def mock_run():
            with open(kwargs['output_path'], 'w') as f:
                f.write(DUMMY_TARBALL['contents'])

        squash = flexmock()
        squash.should_receive('run').replace_with(mock_run)
        flexmock(Squash).new_instances(squash).with_args(Squash, **kwargs)

        flexmock(exit_remove_built_image).should_receive('defer_removal')

    def run_plugin_with_args(self, plugin_args):
        runner = PrePublishPluginsRunner(
            self.tasker,
            self.workflow,
            [{'name': PrePublishSquashPlugin.key, 'args': plugin_args}]
        )

        result = runner.run()
        assert result[PrePublishSquashPlugin.key] is None

        assert self.workflow.exported_image_sequence == [{
            'md5sum': DUMMY_TARBALL['md5sum'],
            'sha256sum': DUMMY_TARBALL['sha256sum'],
            'size': DUMMY_TARBALL['size'],
            'path': self.output_path,
        }]
