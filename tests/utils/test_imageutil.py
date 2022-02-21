"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import pytest
import tarfile
import io
import os

from flexmock import flexmock
from pathlib import Path
from osbs.utils import ImageName

from atomic_reactor import config
from atomic_reactor import util
from atomic_reactor.utils import imageutil, retries


@pytest.fixture
def df_images():
    """DockerfileImages instance for testing."""
    return util.DockerfileImages(["registry.com/fedora:35"])


@pytest.mark.parametrize(
    "image, is_inspectable",
    [
        ("scratch", False),
        ("koji/image-build", False),
        ("registry.com/foo/bar", True),
        # does not work, nobody should ever try to use scratch as an ImageName
        # (ImageName.parse("scratch"), False),
        (ImageName.parse("koji/image-build"), False),
        (ImageName.parse("registry.com/foo/bar"), True),
    ],
)
def test_inspectable(image, is_inspectable):
    assert imageutil.image_is_inspectable(image) == is_inspectable


def mock_tarball(tarball_path, files):
    with tarfile.open(tarball_path, 'w:gz') as tf:
        for filename, file_data in files.items():
            file = tarfile.TarInfo(filename)
            file.size = file_data['size']
            if file_data['content']:
                tf.addfile(file, io.BytesIO(file_data['content']))
            else:
                tf.addfile(file, io.BytesIO(os.urandom(file.size)))


class TestImageUtil:
    """Tests for the ImageUtil class."""

    config = config.Configuration(
        raw_config={
            "version": 1,
            # "registries": [],  # relevant to RegistrySession, not directly relevant to ImageUtil
            "platform_descriptors": [{"platform": "x86_64", "architecture": "amd64"}],
        },
    )

    inspect_data = {"some": "inspect data as returned by RegistryClient.get_inspect_for_image"}

    def mock_get_registry_client(self, expect_image, expect_arch):
        """Make the _get_registry_client method return a fake RegistryClient."""
        registry_client = flexmock()
        (
            registry_client
            .should_receive("get_inspect_for_image")
            .with_args(expect_image, expect_arch)
            .once()
            .and_return(self.inspect_data)
        )
        (
            flexmock(imageutil.ImageUtil)
            .should_receive("_get_registry_client")
            .with_args(expect_image.registry)
            .once()
            .and_return(registry_client)
        )
        return registry_client

    def test_get_inspect_for_image(self, df_images):
        """Test get_inspect_for_image and its caching behavior."""
        image_util = imageutil.ImageUtil(df_images, self.config)
        image = ImageName.parse("registry.com/some-image:1")

        self.mock_get_registry_client(image, expect_arch=None)

        assert image_util.get_inspect_for_image(image) == self.inspect_data
        # check caching (the registry client mock expects its method to be called exactly once,
        #   if imageutil didn't cache the result, it would get called twice)
        assert image_util.get_inspect_for_image(image) == self.inspect_data

        image_as_str = image.to_str()
        # should hit cache regardless of whether you pass a string or an ImageName
        assert image_util.get_inspect_for_image(image_as_str) == self.inspect_data

    @pytest.mark.parametrize(
        "platform, expect_goarch",
        [
            ("x86_64", "amd64"),  # platform is mapped to goarch
            ("s390x", "s390x"),  # platform is not mapped (goarch name is the same)
            ("amd64", "amd64"),  # pass goarch directly
         ],
    )
    def test_get_inspect_for_image_specific_platform(self, platform, expect_goarch, df_images):
        """Test that get_inspect_for_image handles the platform to goarch mapping properly."""
        image_util = imageutil.ImageUtil(df_images, self.config)
        image = ImageName.parse("registry.com/some-image:1")

        # main check: expect_arch
        self.mock_get_registry_client(image, expect_arch=expect_goarch)
        assert image_util.get_inspect_for_image(image, platform) == self.inspect_data

        # should hit cache regardless of whether you pass a platform or a goarch
        assert image_util.get_inspect_for_image(image, expect_goarch) == self.inspect_data

    def test_get_inspect_for_image_not_inspectable(self, df_images):
        """Test that passing a non-inspectable image raises an error."""
        image_util = imageutil.ImageUtil(df_images, self.config)
        custom_image = ImageName.parse("koji/image-build")

        with pytest.raises(ValueError, match=r"ImageName\(.*\) is not inspectable"):
            image_util.get_inspect_for_image(custom_image)

    @pytest.mark.parametrize("platform", [None, "x86_64"])
    def test_base_image_inspect(self, platform, df_images):
        """Test that base_image_inspect just calls get_inspect_for_image with the right args."""
        image_util = imageutil.ImageUtil(df_images, self.config)
        (
            flexmock(image_util)
            .should_receive("get_inspect_for_image")
            # base image in df_images
            .with_args(ImageName.parse("registry.com/fedora:35"), platform)
            .once()
            .and_return(self.inspect_data)
        )
        assert image_util.base_image_inspect(platform) == self.inspect_data

    @pytest.mark.parametrize("base_image", ["scratch", "koji/image-build"])
    def test_base_image_inspect_not_inspectable(self, base_image):
        """Test that inspecting a non-inspectable base image returns an empty dict."""
        image_util = imageutil.ImageUtil(util.DockerfileImages([base_image]), self.config)
        assert image_util.base_image_inspect() == {}

    def test_get_registry_client(self):
        """Test the method that makes a RegistryClient (other tests mock this method)."""
        image_util = imageutil.ImageUtil(util.DockerfileImages([]), self.config)

        registry_session = flexmock()
        (
            flexmock(util.RegistrySession)
            .should_receive("create_from_config")
            .with_args(self.config, "registry.com")
            .once()
            .and_return(registry_session)
        )
        flexmock(util.RegistryClient).should_receive("__init__").with_args(registry_session).once()

        image_util._get_registry_client("registry.com")
        # test caching (i.e. test that the create_from_config method is called only once)
        image_util._get_registry_client("registry.com")

    def test_extract_file_from_image_non_empty_dst_dir(self, tmpdir):
        image_util = imageutil.ImageUtil(util.DockerfileImages([]), self.config)
        image = 'registry.com/fedora:35'
        src_path = '/path/to/file'
        dst_path = Path(tmpdir) / 'dst_dir'
        dst_path.mkdir()
        file = dst_path / 'somefile.txt'
        file.touch()

        with pytest.raises(ValueError, match=f'the destination directory {dst_path} must be empty'):
            image_util.extract_file_from_image(image=image, src_path=src_path, dst_path=dst_path)

    def test_extract_file_from_image_no_file_extracted(self, tmpdir):
        image_util = imageutil.ImageUtil(util.DockerfileImages([]), self.config)
        image = 'registry.com/fedora:35'
        src_path = '/path/to/file'
        dst_path = Path(tmpdir) / 'dst_dir'
        dst_path.mkdir()

        (
            flexmock(retries)
            .should_receive("run_cmd")
            .with_args(['oc', 'image', 'extract', image, '--path', f'{src_path}:{dst_path}'])
            .once()
        )
        with pytest.raises(
                ValueError,
                match=f"Extraction failed, files at path {src_path} not found in the image",
        ):
            image_util.extract_file_from_image(
                image=image, src_path=src_path, dst_path=dst_path
            )

    def test_extract_file_from_image(self, tmpdir):
        image_util = imageutil.ImageUtil(util.DockerfileImages([]), self.config)
        image = 'registry.com/fedora:35'
        src_path = '/path/to/file'
        dst_path = Path(tmpdir) / 'dst_dir'
        dst_path.mkdir()

        # mock the functionality of oc image extract
        # just creates a file in dst_path
        def mock_extract_file(cmd):
            file = dst_path / 'somefile.txt'
            file.touch()

        (
            flexmock(retries)
            .should_receive("run_cmd")
            .with_args(['oc', 'image', 'extract', image, '--path', f'{src_path}:{dst_path}'])
            .replace_with(mock_extract_file).once()
        )
        image_util.extract_file_from_image(image=image, src_path=src_path, dst_path=dst_path)

    def test_download_image_archive_tarball(self):
        image_util = imageutil.ImageUtil(util.DockerfileImages([]), self.config)
        image = 'registry.com/fedora:35'
        path = '/tmp/path'
        (
            flexmock(retries)
            .should_receive("run_cmd")
            .with_args(['skopeo', 'copy', f'docker://{image}', f'docker-archive:{path}'])
            .once()
        )
        image_util.download_image_archive_tarball(image=image, path=path)

    def test_get_uncompressed_image_layer_sizes(self, tmpdir):
        image_util = imageutil.ImageUtil(util.DockerfileImages([]), self.config)
        path = Path(tmpdir) / 'tarball.tar'
        manifest_file_content = (
            '[{"Config":"62700350851fb36b2e770ba33639e9d111616d39fc63da8845a5e53e9ad013de.json",'
            '"RepoTags":[],'
            '"Layers":["92538e92de2938d7c4e279f871107b835bf0c8cc76a5a1655d66855706da18b0.tar"'
            ',"eb7bf34352ca9ba2fb0218870ac3c47b76d0b1fb7d50543d3ecfa497eca242b0.tar",'
            '"6da3b8e0475dcc80515944d0cc3f699429248df6b040f8dd7711e681387185e8.tar",'
            '"07adb74645fe71dec6917e5caca489018edf7ed94f29ac74398eca89c1b9458b.tar"]}]'
        ).encode('utf-8')
        config_file_content = (
            '{"rootfs": {"type": "layers", "diff_ids": '
            '["sha256:92538e92de2938d7c4e279f871107b835bf0c8cc76a5a1655d66855706da18b0", '
            '"sha256:eb7bf34352ca9ba2fb0218870ac3c47b76d0b1fb7d50543d3ecfa497eca242b0", '
            '"sha256:6da3b8e0475dcc80515944d0cc3f699429248df6b040f8dd7711e681387185e8", '
            '"sha256:07adb74645fe71dec6917e5caca489018edf7ed94f29ac74398eca89c1b9458b"]}}'
        ).encode("utf-8")

        mock_files = {
            "92538e92de2938d7c4e279f871107b835bf0c8cc76a5a1655d66855706da18b0.tar": {
                "content": None,
                "size": 1,
            },
            "eb7bf34352ca9ba2fb0218870ac3c47b76d0b1fb7d50543d3ecfa497eca242b0.tar": {
                "content": None,
                "size": 2,
            },
            "6da3b8e0475dcc80515944d0cc3f699429248df6b040f8dd7711e681387185e8.tar": {
                "content": None,
                "size": 3,
            },
            "07adb74645fe71dec6917e5caca489018edf7ed94f29ac74398eca89c1b9458b.tar": {
                "content": None,
                "size": 4,
            },
            "manifest.json": {
                "content": manifest_file_content,
                "size": len(manifest_file_content),
            },
            "62700350851fb36b2e770ba33639e9d111616d39fc63da8845a5e53e9ad013de.json": {
                "content": config_file_content,
                "size": len(config_file_content),
            },
        }

        mock_tarball(tarball_path=path, files=mock_files)

        actual_data = image_util.get_uncompressed_image_layer_sizes(path=path)
        expected_data = [
            {
                "diff_id": "sha256:92538e92de2938d7c4e279f871107b835bf0c8cc76a5a1655d66855706da18b0", # noqa
                "size": 1,
            },
            {
                "diff_id": "sha256:eb7bf34352ca9ba2fb0218870ac3c47b76d0b1fb7d50543d3ecfa497eca242b0", # noqa
                "size": 2,
            },
            {
                "diff_id": "sha256:6da3b8e0475dcc80515944d0cc3f699429248df6b040f8dd7711e681387185e8", # noqa
                "size": 3,
            },
            {
                "diff_id": "sha256:07adb74645fe71dec6917e5caca489018edf7ed94f29ac74398eca89c1b9458b", # noqa
                "size": 4,
            },
        ]

        assert actual_data == expected_data

    def test_get_uncompressed_image_layer_sizes_multiple_entries_in_manifest_json(self, tmpdir):
        image_util = imageutil.ImageUtil(util.DockerfileImages([]), self.config)
        path = Path(tmpdir) / 'tarball.tar'
        manifest_file_content = (
            '[{"Config":"62700350851fb36b2e770ba33639e9d111616d39fc63da8845a5e53e9ad013de.json",'
            '"RepoTags":[],'
            '"Layers":["92538e92de2938d7c4e279f871107b835bf0c8cc76a5a1655d66855706da18b0.tar"'
            ',"eb7bf34352ca9ba2fb0218870ac3c47b76d0b1fb7d50543d3ecfa497eca242b0.tar",'
            '"6da3b8e0475dcc80515944d0cc3f699429248df6b040f8dd7711e681387185e8.tar",'
            '"07adb74645fe71dec6917e5caca489018edf7ed94f29ac74398eca89c1b9458b.tar"]}, '
            '{"Config": "ec3f0931a6e6b6855d76b2d7b0be30e81860baccd891b2e243280bf1cd8ad711.json"'
            ', "RepoTags": [], '
            '"Layers": ["d31505fd5050f6b96ca3268d1db58fc91ae561ddf14eaabc41d63ea2ef8c1c6e.tar"]}]'
        ).encode('utf-8')

        mock_files = {
            "manifest.json": {
                "content": manifest_file_content,
                "size": len(manifest_file_content),
            },
        }

        mock_tarball(tarball_path=path, files=mock_files)

        with pytest.raises(
                ValueError, match="manifest.json file has multiple entries, expected only one"
        ):
            image_util.get_uncompressed_image_layer_sizes(path=path)

    def test_extract_filesystem_layer(self, tmpdir):
        image_util = imageutil.ImageUtil(util.DockerfileImages([]), self.config)
        src_path = Path(tmpdir) / 'tarball.tar'
        dst_path = Path(tmpdir) / 'dst'
        expected_layer_filename = 'd31505fd5050f6b96ca3268d1db58fc91ae561ddf14eaabc41d63ea2ef8c1c6d.tar' # noqa
        manifest_file_content = (
            '[{"Config": "ec3f0931a6e6b6855d76b2d7b0be30e81860baccd891b2e243280bf1cd8ad710.json"'
            ', "RepoTags": [], '
            '"Layers": ["d31505fd5050f6b96ca3268d1db58fc91ae561ddf14eaabc41d63ea2ef8c1c6d.tar"]}]'
        ).encode('utf-8')
        mocked_files = {
            'manifest.json': {'content': manifest_file_content, 'size': len(manifest_file_content)},
            expected_layer_filename: {'content': None, 'size': 1}
        }

        mock_tarball(tarball_path=src_path, files=mocked_files)

        actual_layer_filename = image_util.extract_filesystem_layer(src_path, dst_path)

        assert actual_layer_filename == expected_layer_filename
        assert (dst_path / expected_layer_filename).exists()

    def test_extract_filesystem_layer_more_than_one_layer_fail(self, tmpdir):
        image_util = imageutil.ImageUtil(util.DockerfileImages([]), self.config)
        src_path = Path(tmpdir) / 'tarball.tar'
        dst_path = Path(tmpdir) / 'dst'
        manifest_file_content = (
            '[{"Config":"62700350851fb36b2e770ba33639e9d111616d39fc63da8845a5e53e9ad013de.json",'
            '"RepoTags":[],'
            '"Layers":["92538e92de2938d7c4e279f871107b835bf0c8cc76a5a1655d66855706da18b0.tar"'
            ',"eb7bf34352ca9ba2fb0218870ac3c47b76d0b1fb7d50543d3ecfa497eca242b0.tar",'
            '"6da3b8e0475dcc80515944d0cc3f699429248df6b040f8dd7711e681387185e8.tar",'
            '"07adb74645fe71dec6917e5caca489018edf7ed94f29ac74398eca89c1b9458b.tar"]}]'
        ).encode('utf-8')

        mocked_files = {
            "92538e92de2938d7c4e279f871107b835bf0c8cc76a5a1655d66855706da18b0.tar": {
                "content": None,
                "size": 1,
            },
            "eb7bf34352ca9ba2fb0218870ac3c47b76d0b1fb7d50543d3ecfa497eca242b0.tar": {
                "content": None,
                "size": 2,
            },
            "6da3b8e0475dcc80515944d0cc3f699429248df6b040f8dd7711e681387185e8.tar": {
                "content": None,
                "size": 3,
            },
            "07adb74645fe71dec6917e5caca489018edf7ed94f29ac74398eca89c1b9458b.tar": {
                "content": None,
                "size": 4,
            },
            "manifest.json": {
                "content": manifest_file_content,
                "size": len(manifest_file_content),
            },
        }

        mock_tarball(tarball_path=src_path, files=mocked_files)

        with pytest.raises(ValueError, match=f'Tarball at {src_path} has more than 1 layer'):
            image_util.extract_filesystem_layer(src_path, dst_path)

    def test_extract_filesystem_layer_multiple_entries_in_manifest_json(self, tmpdir):
        image_util = imageutil.ImageUtil(util.DockerfileImages([]), self.config)
        src_path = Path(tmpdir) / 'tarball.tar'
        dst_path = Path(tmpdir) / 'dst'
        expected_layer_filename = 'd31505fd5050f6b96ca3268d1db58fc91ae561ddf14eaabc41d63ea2ef8c1c6d.tar' # noqa
        manifest_file_content = (
            '[{"Config": "ec3f0931a6e6b6855d76b2d7b0be30e81860baccd891b2e243280bf1cd8ad710.json"'
            ', "RepoTags": [], '
            '"Layers": ["d31505fd5050f6b96ca3268d1db58fc91ae561ddf14eaabc41d63ea2ef8c1c6d.tar"]},'
            '{"Config": "ec3f0931a6e6b6855d76b2d7b0be30e81860baccd891b2e243280bf1cd8ad711.json"'
            ', "RepoTags": [], '
            '"Layers": ["d31505fd5050f6b96ca3268d1db58fc91ae561ddf14eaabc41d63ea2ef8c1c6e.tar"]}]'
        ).encode("utf-8")

        mocked_files = {
            'manifest.json': {'content': manifest_file_content, 'size': len(manifest_file_content)},
            expected_layer_filename: {'content': None, 'size': 1}
        }

        mock_tarball(tarball_path=src_path, files=mocked_files)

        with pytest.raises(
                ValueError, match="manifest.json file has multiple entries, expected only one"
        ):
            image_util.extract_filesystem_layer(src_path, dst_path)
