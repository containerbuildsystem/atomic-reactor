import os
import tarfile

import pytest

from atomic_reactor.constants import EXPORTED_COMPRESSED_IMAGE_NAME_TEMPLATE
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner
from atomic_reactor.plugins.post_compress import CompressPlugin
from atomic_reactor.util import ImageName

from tests.constants import INPUT_IMAGE, MOCK

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
    @pytest.mark.parametrize('method, load_exported_image, extension', [
        ('gzip', False, 'gz'),
        ('lzma', False, 'xz'),
        ('gzip', True, 'gz'),
    ])
    def test_compress(self, tmpdir, caplog, method, load_exported_image, extension):
        if MOCK:
            mock_docker()

        tasker = DockerTasker()
        workflow = DockerBuildWorkflow({'provider': 'git', 'uri': 'asd'}, 'test-image')
        workflow.builder = X()
        exp_img = os.path.join(str(tmpdir), 'img.tar')

        if load_exported_image:
            tarfile.open(exp_img, mode='w').close()
            workflow.exported_image_sequence.append({'path': exp_img})

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

        runner.run()

        compressed_img = os.path.join(
            workflow.source.tmpdir,
            EXPORTED_COMPRESSED_IMAGE_NAME_TEMPLATE.format(extension))
        assert os.path.exists(compressed_img)
        metadata = workflow.exported_image_sequence[-1]
        assert metadata['path'] == compressed_img
        assert 'uncompressed_size' in metadata
        assert isinstance(metadata['uncompressed_size'], int)
        assert ", ratio: " in caplog.text()
