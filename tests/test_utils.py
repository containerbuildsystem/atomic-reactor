import os
import docker
from dock.util import ImageName, get_baseimage_from_dockerfile, wait_for_command, \
                      clone_git_repo, LazyGit, figure_out_dockerfile
from tests.constants import DOCKERFILE_GIT, INPUT_IMAGE, MOCK

if MOCK:
    from tests.docker_mock import mock_docker

TEST_DATA = {
    "repository.com/image-name": ImageName(registry="repository.com", repo="image-name"),
    "repository.com/prefix/image-name:1": ImageName(registry="repository.com",
                                                    namespace="prefix",
                                                    repo="image-name", tag="1"),
    "repository.com/prefix/image-name": ImageName(registry="repository.com",
                                                  namespace="prefix",
                                                  repo="image-name"),
    "image-name": ImageName(repo="image-name"),
    "registry:5000/image-name:latest": ImageName(registry="registry:5000",
                                                 repo="image-name", tag="latest"),
    "registry:5000/image-name": ImageName(registry="registry:5000", repo="image-name"),
    "fedora:20": ImageName(repo="fedora", tag="20"),
    "prefix/image-name:1": ImageName(namespace="prefix", repo="image-name", tag="1"),
    }

def test_image_name_parse():
    for inp, parsed in TEST_DATA.items():
        assert ImageName.parse(inp) == parsed

def test_image_name_format():
    for expected, image_name in TEST_DATA.items():
        assert image_name.to_str() == expected


def test_wait_for_command():
    if MOCK:
        mock_docker()

    d = docker.Client()
    logs_gen = d.pull(INPUT_IMAGE, stream=True)
    assert wait_for_command(logs_gen) is not None


def test_clone_git_repo(tmpdir):
    tmpdir_path = str(tmpdir.realpath())
    clone_git_repo(DOCKERFILE_GIT, tmpdir_path)
    assert os.path.isdir(os.path.join(tmpdir_path, '.git'))


def test_get_baseimg_from_df(tmpdir):
    tmpdir_path = str(tmpdir.realpath())
    clone_git_repo(DOCKERFILE_GIT, tmpdir_path)
    base_img = get_baseimage_from_dockerfile(tmpdir_path)
    assert base_img.startswith('fedora')


def test_figure_out_dockerfile(tmpdir):
    tmpdir_path = str(tmpdir.realpath())
    clone_git_repo(DOCKERFILE_GIT, tmpdir_path)
    path, dir = figure_out_dockerfile(tmpdir_path)
    assert os.path.isfile(path)
    assert os.path.isdir(dir)


def test_lazy_git():
    lazy_git = LazyGit(git_url=DOCKERFILE_GIT)
    with lazy_git:
        assert lazy_git.git_path is not None


def test_lazy_git_with_tmpdir(tmpdir):
    t = str(tmpdir.realpath())
    lazy_git = LazyGit(git_url=DOCKERFILE_GIT, tmpdir=t)
    assert lazy_git._tmpdir == t
    assert lazy_git.git_path is not None
