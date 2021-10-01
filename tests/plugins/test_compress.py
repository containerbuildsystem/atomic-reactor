import os
import tarfile

import pytest

from atomic_reactor.constants import (EXPORTED_COMPRESSED_IMAGE_NAME_TEMPLATE,
                                      IMAGE_TYPE_DOCKER_ARCHIVE)
from atomic_reactor.plugin import PostBuildPluginsRunner
from atomic_reactor.plugins.post_compress import CompressPlugin
from atomic_reactor.inner import BuildResult


class TestCompress(object):
    @pytest.mark.skip(reason="plugin has to fetch image differently than via docker")
    @pytest.mark.parametrize('source_build', (True, False))
    @pytest.mark.parametrize('method, load_exported_image, give_export, extension', [
        ('gzip', False, True, 'gz'),
        ('lzma', False, False, 'xz'),
        ('gzip', True, True, 'gz'),
        ('gzip', True, False, 'gz'),
        ('spam', True, True, None),
    ])
    def test_compress(self, tmpdir, caplog, workflow,
                      source_build, method,
                      load_exported_image, give_export, extension):
        exp_img = os.path.join(str(tmpdir), 'img.tar')

        if source_build:
            workflow.build_result = BuildResult(source_docker_archive="oci_path")
        else:
            workflow.build_result = BuildResult(image_id="12345")

        if load_exported_image and give_export:
            tarfile.open(exp_img, mode='w').close()
            workflow.exported_image_sequence.append({'path': exp_img,
                                                     'type': IMAGE_TYPE_DOCKER_ARCHIVE})

        runner = PostBuildPluginsRunner(
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
                workflow.source.workdir,
                EXPORTED_COMPRESSED_IMAGE_NAME_TEMPLATE.format(extension))
            assert os.path.exists(compressed_img)
            metadata = workflow.exported_image_sequence[-1]
            assert metadata['path'] == compressed_img
            assert metadata['type'] == IMAGE_TYPE_DOCKER_ARCHIVE
            assert 'uncompressed_size' in metadata
            assert isinstance(metadata['uncompressed_size'], int)
            assert ", ratio: " in caplog.text

    def test_skip_plugin(self, caplog, workflow):
        workflow.user_params['scratch'] = True

        runner = PostBuildPluginsRunner(
            workflow,
            [{
                'name': CompressPlugin.key,
                'args': {
                    'method': 'gzip',
                    'load_exported_image': True,
                },
            }]
        )

        runner.run()
        assert 'scratch build, skipping plugin' in caplog.text
