"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import pytest
from flexmock import flexmock
from osbs.utils import ImageName

from atomic_reactor import config
from atomic_reactor import util
from atomic_reactor.utils import imageutil


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
