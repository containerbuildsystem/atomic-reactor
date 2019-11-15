from __future__ import absolute_import

import os
import tarfile

import pytest

from atomic_reactor.constants import (EXPORTED_COMPRESSED_IMAGE_NAME_TEMPLATE,
                                      IMAGE_TYPE_DOCKER_ARCHIVE)
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner
from atomic_reactor.plugins.post_compress import CompressPlugin
from atomic_reactor.util import ImageName
from atomic_reactor.build import BuildResult

from tests.constants import INPUT_IMAGE, MOCK

from six import integer_types

if MOCK:
    from tests.docker_mock import mock_docker


class Y(object):
    dockerfile_path = None
    path = None


class X(object):
    image_id = INPUT_IMAGE
    source = Y()
    base_image = ImageName.parse('asd')


class TestCompress(object):
    @pytest.mark.parametrize('source_build', (True, False))
    @pytest.mark.parametrize('method, load_exported_image, give_export, extension', [
        ('gzip', False, True, 'gz'),
        ('lzma', False, False, 'xz'),
        ('gzip', True, True, 'gz'),
        ('gzip', True, False, 'gz'),
        ('spam', True, True, None),
    ])
    def test_compress(self, tmpdir, caplog, source_build, method,
                      load_exported_image, give_export, extension):
        if MOCK:
            mock_docker()

        tasker = DockerTasker()
        workflow = DockerBuildWorkflow(
            'test-image',
            source={'provider': 'git', 'uri': 'asd'}
        )
        workflow.builder = X()
        exp_img = os.path.join(str(tmpdir), 'img.tar')

        if source_build:
            workflow.build_result = BuildResult(oci_image_path="oci_path")
        else:
            workflow.build_result = BuildResult(image_id="12345")

        if load_exported_image and give_export:
            tarfile.open(exp_img, mode='w').close()
            workflow.exported_image_sequence.append({'path': exp_img,
                                                     'type': IMAGE_TYPE_DOCKER_ARCHIVE})
            tasker = None  # image provided, should not query docker

        runner = PostBuildPluginsRunner(
            tasker,
            workflow,
            [{
                'name': CompressPlugin.key,
                'args': {
                    'method': method,
                    'load_exported_image': load_exported_image,
                },
            }]
        )

        if not extension:
            with pytest.raises(Exception) as excinfo:
                runner.run()
            assert 'Unsupported compression format' in str(excinfo.value)
            return

        runner.run()

        if source_build and not (give_export and load_exported_image):
            assert 'skipping, no exported source image to compress' in caplog.text
        else:
            compressed_img = os.path.join(
                workflow.source.tmpdir,
                EXPORTED_COMPRESSED_IMAGE_NAME_TEMPLATE.format(extension))
            assert os.path.exists(compressed_img)
            metadata = workflow.exported_image_sequence[-1]
            assert metadata['path'] == compressed_img
            assert metadata['type'] == IMAGE_TYPE_DOCKER_ARCHIVE
            assert 'uncompressed_size' in metadata
            assert isinstance(metadata['uncompressed_size'], integer_types)
            assert ", ratio: " in caplog.text
