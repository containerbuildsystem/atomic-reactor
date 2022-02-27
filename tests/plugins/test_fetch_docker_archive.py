import os
import tarfile

import pytest

from atomic_reactor.constants import (EXPORTED_COMPRESSED_IMAGE_NAME_TEMPLATE,
                                      IMAGE_TYPE_DOCKER_ARCHIVE)
from atomic_reactor.plugin import PostBuildPluginsRunner
from atomic_reactor.plugins.post_fetch_docker_archive import FetchDockerArchivePlugin
from atomic_reactor.inner import BuildResult


class TestFetchDockerArchive(object):
    @pytest.mark.parametrize('source_build', (True, False))
    def test_fetch_docker_archive(self, tmpdir, caplog, workflow, source_build):
        exp_img = os.path.join(str(tmpdir), 'img.tar')

        if source_build:
            workflow.data.build_result = BuildResult(source_docker_archive="oci_path")
        else:
            workflow.data.build_result = BuildResult(image_id="12345")

        runner = PostBuildPluginsRunner(
            workflow,
            [{
                'name': FetchDockerArchivePlugin.key,
                'args': {
                },
            }]
        )

        runner.run()

        if source_build:
            assert 'skipping, no exported source image' in caplog.text
        else:
            img = os.path.join(
                workflow.source.workdir,
                EXPORTED_COMPRESSED_IMAGE_NAME_TEMPLATE)
            assert os.path.exists(img)
            metadata = workflow.data.exported_image_sequence[-1]
            assert metadata['path'] == img
            assert metadata['type'] == IMAGE_TYPE_DOCKER_ARCHIVE

    def test_skip_plugin(self, caplog, workflow):
        workflow.user_params['scratch'] = True

        runner = PostBuildPluginsRunner(
            workflow,
            [{
                'name': FetchDockerArchivePlugin.key,
                'args': {
                },
            }]
        )

        runner.run()
        assert 'scratch build, skipping plugin' in caplog.text
