"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import logging
import os
import pytest

from flexmock import flexmock

from atomic_reactor.constants import EXPORTED_SQUASHED_IMAGE_NAME, IMAGE_TYPE_DOCKER_ARCHIVE
from atomic_reactor.inner import BuildResult
from atomic_reactor.plugin import PrePublishPluginsRunner, PluginFailedException
from atomic_reactor.plugins import exit_remove_built_image
from atomic_reactor.plugins.prepub_squash import PrePublishSquashPlugin
from atomic_reactor.util import DockerfileImages
from atomic_reactor.utils import imageutil
from docker_squash.squash import Squash


DUMMY_TARBALL = {
    'contents': 'dummy file contents',
    'md5sum': '79cad6cda5ebe6b9bdbdbb6a56587e28',
    'sha256sum': '5fefd3ff57b97c856958bfc0333231f4f8a600b305d749c9616b0879765f2472',
    'size': 19
}
BASE_IMAGE_ID = '3ab9a7ed8a169ab89b09fb3e12a14a390d3c662703b65b4541c0c7bde0ee97eb'
SET_DEFAULT_LAYER_ID = object()


@pytest.fixture
def workflow(workflow):
    workflow.dockerfile_images = DockerfileImages(['Fedora:22'])
    workflow.image_id = 'image_id'
    flexmock(workflow, image='image')
    (flexmock(imageutil)
        .should_receive('base_image_inspect')
        .and_return({'Id': BASE_IMAGE_ID}))
    return workflow


@pytest.mark.usefixtures('user_params')
class TestSquashPlugin(object):
    output_path = None

    def setup_method(self, method):
        # Expected path for exported squashed image.
        self.output_path = None

    def test_skip_squash(self, workflow):
        flexmock(Squash).should_receive('__init__').never()
        workflow.build_result = BuildResult(image_id="spam", skip_layer_squash=True)
        self.run_plugin_with_args(workflow, {})

    @pytest.mark.parametrize('base_from_scratch', (True, False))
    def test_default_parameters(self, base_from_scratch, workflow):
        self.should_squash_with_kwargs(workflow, base_from_scratch=base_from_scratch)
        if base_from_scratch:
            workflow.dockerfile_images = DockerfileImages(['scratch'])
        self.run_plugin_with_args(workflow, {})

    @pytest.mark.parametrize(('plugin_tag', 'squash_tag'), (
        ('spam', 'spam'),
        (None, 'image'),
        ('', 'image'),
    ))
    def test_tag_value_is_used(self, plugin_tag, squash_tag, workflow):
        self.should_squash_with_kwargs(workflow, tag=squash_tag)
        self.run_plugin_with_args(workflow, {'tag': plugin_tag})

    @pytest.mark.parametrize('dont_load', (True, False))
    def test_dont_load_is_honored(self, dont_load, workflow):
        self.should_squash_with_kwargs(workflow, load_image=not dont_load)
        self.run_plugin_with_args(workflow, {'dont_load': dont_load})

    @pytest.mark.parametrize(('from_base', 'from_layer', 'squash_from_layer'), (
        (False, 'from-layer', 'from-layer'),
        (True, 'from-layer', 'from-layer'),
        (False, None, None),
        (True, None, SET_DEFAULT_LAYER_ID),
    ))
    def test_from_specified(self, from_base, from_layer, squash_from_layer, workflow):
        self.should_squash_with_kwargs(workflow, from_layer=squash_from_layer)
        self.run_plugin_with_args(workflow, {'from_base': from_base, 'from_layer': from_layer})

    def test_missing_base_image_id(self, workflow):
        self.should_squash_with_kwargs(workflow, from_layer=None)
        with pytest.raises(PluginFailedException):
            self.run_plugin_with_args(workflow, {'from_layer': None})

    @pytest.mark.parametrize('new_id,expected_id', [
        ('abcdef', 'sha256:abcdef'),
        ('sha256:abcdef', 'sha256:abcdef'),
    ])
    def test_sha256_prefix(self, new_id, expected_id, workflow):
        self.should_squash_with_kwargs(workflow, new_id=new_id)
        self.run_plugin_with_args(workflow, {})
        assert workflow.image_id == expected_id

    def test_skip_saving_archive(self, workflow):
        self.should_squash_with_kwargs(workflow, output_path=None)
        self.run_plugin_with_args(workflow, {'save_archive': False})

    def test_skip_plugin(self, caplog, workflow):
        self.should_squash_with_kwargs(workflow, output_path=None)
        workflow.user_params = {'flatpak': True}
        self.run_plugin_with_args(workflow, {'save_archive': False})
        assert 'flatpak build, skipping plugin' in caplog.text

    def should_squash_with_kwargs(self, workflow, new_id='abc', base_from_scratch=False, **kwargs):
        kwargs.setdefault('image', workflow.image_id)
        kwargs.setdefault('load_image', True)
        kwargs.setdefault('log', logging.Logger)
        kwargs.setdefault('output_path', os.path.join(workflow.source.workdir,
                                                      EXPORTED_SQUASHED_IMAGE_NAME))
        kwargs.setdefault('tag', workflow.image)

        # Avoid inspect errors at this point
        if 'from_layer' not in kwargs:
            kwargs['from_layer'] = BASE_IMAGE_ID

        self.output_path = kwargs['output_path']

        if kwargs.get('from_layer') == SET_DEFAULT_LAYER_ID:
            kwargs['from_layer'] = BASE_IMAGE_ID

        if base_from_scratch:
            kwargs['from_layer'] = None

        def mock_run():
            if not kwargs['output_path'] is None:
                with open(kwargs['output_path'], 'w') as f:
                    f.write(DUMMY_TARBALL['contents'])

            return new_id

        flexmock(Squash).should_receive('run').replace_with(mock_run)
        flexmock(Squash).should_receive('__init__').with_args(**kwargs)

        flexmock(exit_remove_built_image).should_receive('defer_removal')

    def run_plugin_with_args(self, workflow, plugin_args):
        runner = PrePublishPluginsRunner(
            workflow,
            [{'name': PrePublishSquashPlugin.key, 'args': plugin_args}]
        )

        result = runner.run()
        assert result[PrePublishSquashPlugin.key] is None

        if self.output_path:
            assert workflow.exported_image_sequence == [{
                'md5sum': DUMMY_TARBALL['md5sum'],
                'sha256sum': DUMMY_TARBALL['sha256sum'],
                'size': DUMMY_TARBALL['size'],
                'type': IMAGE_TYPE_DOCKER_ARCHIVE,
                'path': self.output_path,
            }]
        else:
            assert workflow.exported_image_sequence == []
