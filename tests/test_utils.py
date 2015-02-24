import os
import docker
from dock.util import split_repo_img_name_tag, join_repo_img_name_tag, get_baseimage_from_dockerfile, \
    join_repo_img_name, join_img_name_tag, wait_for_command, clone_git_repo, LazyGit, figure_out_dockerfile
from tests.constants import DOCKERFILE_GIT, INPUT_IMAGE


TEST_DATA = [
    ("repository.com/image-name", ("repository.com", "image-name", "")),
    ("repository.com/prefix/image-name:1", ("repository.com", "prefix/image-name", "1")),
    ("image-name", ("", "image-name", "")),
    ("registry:5000/image-name:latest", ("registry:5000", "image-name", 'latest')),
    ("fedora:20", ("", "fedora", "20")),
]


TEST_DATA_IMG_TAG = [
    ("image-name", ("image-name", "")),
    ("prefix/image-name:1", ("prefix/image-name", "1")),
    ("fedora:20", ("fedora", "20")),
]


TEST_DATA_REG_IMG = [
    ("repository.com/image-name", ("repository.com", "image-name")),
    ("repository.com/prefix/image-name", ("repository.com", "prefix/image-name")),
    ("image-name", ("", "image-name")),
    ("registry:5000/image-name", ("registry:5000", "image-name")),
]


def test_split_image_repo_name():
    global TEST_DATA
    for chain, chunks in TEST_DATA:
        result = split_repo_img_name_tag(chain)
        assert result == chunks


def test_join_repo_img_name_tag():
    global TEST_DATA
    for chain, chunks in TEST_DATA:
        result = join_repo_img_name_tag(*chunks)
        assert result == chain


def test_join_reg_img():
    global TEST_DATA_REG_IMG
    for chain, chunks in TEST_DATA_REG_IMG:
        result = join_repo_img_name(*chunks)
        assert result == chain


def test_join_img_tag():
    global TEST_DATA_IMG_TAG
    for chain, chunks in TEST_DATA_IMG_TAG:
        result = join_img_name_tag(*chunks)
        assert result == chain


def test_wait_for_command():
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
