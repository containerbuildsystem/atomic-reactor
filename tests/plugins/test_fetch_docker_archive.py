import os

from flexmock import flexmock

from atomic_reactor.constants import EXPORTED_SQUASHED_IMAGE_NAME, IMAGE_TYPE_DOCKER_ARCHIVE
from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugins.fetch_docker_archive import FetchDockerArchivePlugin
from atomic_reactor.utils.imageutil import ImageUtil
from tests.mock_env import MockEnv


class TestFetchDockerArchive(object):

    def create_image(self, build_dir: BuildDir):
        exp_img = build_dir.path / 'image.tar'
        with open(exp_img, 'w') as f:
            f.write('test')

    def test_fetch_docker_archive(self, tmpdir, caplog, workflow):
        platforms = ['x86_64', 's390x', 'ppc64le', 'aarch64']

        workflow.build_dir.init_build_dirs(platforms, workflow.source)
        workflow.data.tag_conf.add_unique_image('registry.com/image:latest')

        workflow.build_dir.for_each_platform(self.create_image)
        flexmock(ImageUtil).should_receive('download_image_archive_tarball').times(4)

        results = (MockEnv(workflow)
                   .for_plugin(FetchDockerArchivePlugin.key)
                   .create_runner()
                   .run())

        for platform, metadata in results[FetchDockerArchivePlugin.key].items():
            image_path = workflow.build_dir.path / platform
            img = os.path.join(image_path / EXPORTED_SQUASHED_IMAGE_NAME)
            assert os.path.exists(img)
            assert metadata['path'] == img
            assert metadata['type'] == IMAGE_TYPE_DOCKER_ARCHIVE
            assert f'image for platform:{platform} available at ' \
                   f"{image_path / 'image.tar'}" in caplog.text
