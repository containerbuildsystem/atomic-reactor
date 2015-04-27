from dock.build import InsideBuilder
from dock.core import DockerTasker
from dock.util import ImageName
from constants import LOCALHOST_REGISTRY, DOCKERFILE_GIT

#
# This stuff is used in tests; you have to have internet connection,
# running registry on port 5000 and it helps if you've pulled fedora:latest before
git_base_repo = "fedora"
git_base_tag = "latest"
git_base_image = ImageName(registry=LOCALHOST_REGISTRY, repo="fedora", tag="latest")


def test_pull_base_image(tmpdir):
    t = DockerTasker()
    b = InsideBuilder(DOCKERFILE_GIT, "", tmpdir=str(tmpdir))
    reg_img_name = b.pull_base_image(LOCALHOST_REGISTRY, insecure=True)
    reg_img_name = ImageName.parse(reg_img_name)
    assert t.inspect_image(reg_img_name) is not None
    assert reg_img_name.repo == git_base_image.repo
    assert reg_img_name.tag == git_base_image.tag
    # clean
    t.remove_image(git_base_image)


def test_build_image(tmpdir):
    t = DockerTasker()
    provided_image = "test-build:test_tag"
    b = InsideBuilder(DOCKERFILE_GIT, provided_image, tmpdir=str(tmpdir))
    build_result = b.build()
    assert t.inspect_image(build_result.image_id)
    # clean
    t.remove_image(build_result.image_id)


def test_build_error_dockerfile(tmpdir):
    t = DockerTasker()
    provided_image = "test-build:test_tag"
    b = InsideBuilder(DOCKERFILE_GIT, provided_image, git_commit="error-build", tmpdir=str(tmpdir))
    build_result = b.build()
    assert build_result.is_failed()


def test_inspect_built_image(tmpdir):
    t = DockerTasker()
    provided_image = "test-build:test_tag"
    b = InsideBuilder(DOCKERFILE_GIT, provided_image, tmpdir=str(tmpdir))
    build_result = b.build()

    built_inspect = b.inspect_built_image()

    assert built_inspect is not None
    assert built_inspect["Id"] is not None

    # clean
    t.remove_image(build_result.image_id)


def test_inspect_base_image(tmpdir):
    b = InsideBuilder(DOCKERFILE_GIT, '', tmpdir=str(tmpdir))

    built_inspect = b.inspect_base_image()

    assert built_inspect is not None
    assert built_inspect["Id"] is not None


def test_get_base_image_info(tmpdir):
    b = InsideBuilder(DOCKERFILE_GIT, '', tmpdir=str(tmpdir))

    built_inspect = b.get_base_image_info()

    assert built_inspect is not None
    assert built_inspect["Id"] is not None
    assert built_inspect["RepoTags"] is not None
