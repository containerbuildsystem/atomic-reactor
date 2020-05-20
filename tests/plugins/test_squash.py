"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import logging
import os
import pytest

from flexmock import flexmock

from atomic_reactor.constants import EXPORTED_SQUASHED_IMAGE_NAME, IMAGE_TYPE_DOCKER_ARCHIVE
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PrePublishPluginsRunner, PluginFailedException
from atomic_reactor.plugins import exit_remove_built_image
from atomic_reactor.plugins.prepub_squash import PrePublishSquashPlugin
from osbs.utils import ImageName
from atomic_reactor.build import BuildResult
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
        self.tasker = DockerTasker(retry_times=0)
        self.base_image = ImageName(repo='Fedora', tag='22')
        self.base_from_scratch = False
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

    @property
    def base_image_inspect(self):
        return self.tasker.inspect_image(self.base_image)


class TestSquashPlugin(object):
    workflow = None
    tasker = None
    output_path = None

    def setup_method(self, method):
        if MOCK:
            mock_docker()
        self.workflow = DockerBuildWorkflow('test-image', source=MOCK_SOURCE)
        self.workflow.builder = MockInsideBuilder()
        self.tasker = self.workflow.builder.tasker

        # Expected path for exported squashed image.
        self.output_path = None

    def test_skip_squash(self):
        flexmock(Squash).should_receive('__init__').never()
        self.workflow.build_result = BuildResult(image_id="spam", skip_layer_squash=True)
        self.run_plugin_with_args({})

    @pytest.mark.parametrize('base_from_scratch', (True, False))
    def test_default_parameters(self, base_from_scratch):
        self.should_squash_with_kwargs(base_from_scratch=base_from_scratch)
        self.workflow.builder.base_from_scratch = base_from_scratch
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

    @pytest.mark.parametrize('new_id,expected_id', [
        ('abcdef', 'sha256:abcdef'),
        ('sha256:abcdef', 'sha256:abcdef'),
    ])
    def test_sha256_prefix(self, new_id, expected_id):
        if MOCK:
            mock_docker()
        self.should_squash_with_kwargs(new_id=new_id)
        self.run_plugin_with_args({})
        assert self.workflow.builder.image_id == expected_id

    def test_skip_saving_archive(self):
        self.should_squash_with_kwargs(output_path=None)
        self.run_plugin_with_args({'save_archive': False})

    def test_skip_plugin(self, caplog):
        self.should_squash_with_kwargs(output_path=None)
        self.workflow.user_params = {'flatpak': True}
        self.run_plugin_with_args({'save_archive': False})
        assert 'flatpak build, skipping plugin' in caplog.text

    def should_squash_with_kwargs(self, new_id='abc', base_from_scratch=False, **kwargs):
        kwargs.setdefault('image', self.workflow.builder.image_id)
        kwargs.setdefault('load_image', True)
        kwargs.setdefault('log', logging.Logger)
        kwargs.setdefault('output_path', os.path.join(self.workflow.source.workdir,
                                                      EXPORTED_SQUASHED_IMAGE_NAME))
        kwargs.setdefault('tag', self.workflow.builder.image)

        # Avoid inspect errors at this point
        if 'from_layer' not in kwargs:
            kwargs['from_layer'] = self.workflow.builder.base_image_inspect['Id']

        self.output_path = kwargs['output_path']

        if kwargs.get('from_layer') == SET_DEFAULT_LAYER_ID:
            kwargs['from_layer'] = self.workflow.builder.base_image_inspect['Id']

        if base_from_scratch:
            kwargs['from_layer'] = None

        def mock_run():
            if not kwargs['output_path'] is None:
                with open(kwargs['output_path'], 'w') as f:
                    f.write(DUMMY_TARBALL['contents'])

            return new_id

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

        if self.output_path:
            assert self.workflow.exported_image_sequence == [{
                'md5sum': DUMMY_TARBALL['md5sum'],
                'sha256sum': DUMMY_TARBALL['sha256sum'],
                'size': DUMMY_TARBALL['size'],
                'type': IMAGE_TYPE_DOCKER_ARCHIVE,
                'path': self.output_path,
            }]
        else:
            assert self.workflow.exported_image_sequence == []
