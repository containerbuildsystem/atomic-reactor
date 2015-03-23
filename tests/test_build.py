from dock.build import InsideBuilder
from dock.core import DockerTasker


#
# This stuff is used in tests; you have to have internet connection,
# running registry on port 5000 and it helps if you've pulled fedora:latest before
git_url = "https://github.com/TomasTomecek/docker-hello-world.git"
local_registry = "localhost:5000"
git_base_image = "fedora:latest"


def test_pull_base_image(tmpdir):
    t = DockerTasker()
    b = InsideBuilder(git_url, "", tmpdir=str(tmpdir))
    reg_img_name = b.pull_base_image(local_registry, insecure=True)
    assert t.inspect_image(reg_img_name) is not None
    assert reg_img_name == git_base_image
    # clean
    t.remove_image(local_registry + '/' + git_base_image)


def test_build_image(tmpdir):
    t = DockerTasker()
    provided_image = "test-build:test_tag"
    b = InsideBuilder(git_url, provided_image, tmpdir=str(tmpdir))
    received_image = b.build()
    assert provided_image == received_image
    assert t.inspect_image(provided_image)
    # clean
    t.remove_image(received_image)


def test_inspect_built_image(tmpdir):
    t = DockerTasker()
    provided_image = "test-build:test_tag"
    b = InsideBuilder(git_url, provided_image, tmpdir=str(tmpdir))
    received_image = b.build()

    built_inspect = b.inspect_built_image()

    assert built_inspect is not None
    assert built_inspect["Id"] is not None

    # clean
    t.remove_image(received_image)


def test_inspect_base_image(tmpdir):
    b = InsideBuilder(git_url, '', tmpdir=str(tmpdir))

    built_inspect = b.inspect_base_image()

    assert built_inspect is not None
    assert built_inspect["Id"] is not None


def test_get_base_image_info(tmpdir):
    b = InsideBuilder(git_url, '', tmpdir=str(tmpdir))

    built_inspect = b.get_base_image_info()

    assert built_inspect is not None
    assert built_inspect["Id"] is not None
    assert built_inspect["RepoTags"] is not None
