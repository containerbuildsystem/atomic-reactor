import os
import shutil
import tempfile
import logging
import git

__author__ = 'ttomecek'

logger = logging.getLogger(__name__)


def split_repo_img_name_tag(image):
    """
    registry.com/image:tag -> (registry, image, tag)

    returns blank strings for missing fields
    """
    try:
        reg_uri, img_name = image.split('/', 1)
    except ValueError:
        img_name = image
        reg_uri = ""
    try:
        img_name, tag = img_name.rsplit(":", 1)
    except ValueError:
        tag = ""
    return reg_uri, img_name, tag


def join_repo_img_name(reg_uri, img_name):
    """ ('registry', 'image_name') -> "registry/image_name" """
    if not img_name:
        raise RuntimeError("No image specified")
    if reg_uri:
        if not reg_uri.endswith('/'):
            reg_uri += '/'
        return reg_uri + img_name
    else:
        return img_name


def join_img_name_tag(img_name, tag):
    """ (image_name, tag) -> "image_name:tag" """
    if not img_name:
        raise RuntimeError("No image specified")
    response = img_name
    if tag:
        response = "%s:%s" % (response, tag)
    return response


def join_repo_img_name_tag(reg_uri, img_name, tag):
    """ (image_name, registry, tag) -> "registry/image_name:tag" """
    if not img_name:
        raise RuntimeError("No image specified")
    response = join_repo_img_name(reg_uri, img_name)
    return join_img_name_tag(response, tag)


def get_baseimage_from_dockerfile_path(path):
    with open(path, 'r') as dockerfile:
        for line in dockerfile:
            if line.startswith("FROM"):
                return line.split()[1]


def get_baseimage_from_dockerfile(git_path, path=None):
    """ return name of base image from provided gitrepo """
    if path:
        if path.endswith('Dockerfile'):
            dockerfile_path = os.path.join(git_path, path)
        else:
            dockerfile_path = os.path.join(git_path, path, 'Dockerfile')
    else:
        dockerfile_path = os.path.join(git_path, 'Dockerfile')
    return get_baseimage_from_dockerfile_path(dockerfile_path)


def wait_for_command(logs_generator):
    """
    using given generator, wait for it to raise StopIteration, which
    indicates that docker has finished with processing

    :return: list of str, logs
    """
    logs = []
    while True:
        try:
            item = logs_generator.next()
            item = item.strip()
            logger.debug(item)
            logs.append(item)
        except StopIteration:
            break
    return logs


class LazyGit(object):
    def __init__(self, git_url, tmpdir=None):
        self.git_url = git_url
        self.provided_tmpdir = tmpdir
        self._git_path = None

    @property
    def _tmpdir(self):
        return self.provided_tmpdir or self.our_tmpdir

    @property
    def git_path(self):
        if self._git_path is None:
            git.Repo.clone_from(self.git_url, self._tmpdir)
        return self._tmpdir

    def __enter__(self):
        if not self.provided_tmpdir:
            self.our_tmpdir = tempfile.mkdtemp()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.provided_tmpdir:
            if self.our_tmpdir:
                shutil.rmtree(self.our_tmpdir)
