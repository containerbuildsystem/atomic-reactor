from __future__ import print_function, unicode_literals

import json
import os
import shutil
import tempfile
import logging
import git
from dock.constants import DOCKERFILE_FILENAME

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


def get_baseimage_from_dockerfile(git_path, path=''):
    """ return name of base image from provided gitrepo """
    if git_path.endswith(DOCKERFILE_FILENAME):
        dockerfile_path = git_path
    else:
        if path.endswith(DOCKERFILE_FILENAME):
            dockerfile_path = os.path.join(git_path, path)
        else:
            dockerfile_path = os.path.join(git_path, path, DOCKERFILE_FILENAME)
    return get_baseimage_from_dockerfile_path(dockerfile_path)


def wait_for_command(logs_generator):
    """
    using given generator, wait for it to raise StopIteration, which
    indicates that docker has finished with processing

    :return: list of str, logs
    """
    logger.info("wait_for_command")
    logs = []
    while True:
        try:
            parsed_item = None
            item = next(logs_generator)  # py2 & 3 compat
            item = item.decode("utf-8")
            try:
                parsed_item = json.loads(item)
            except ValueError:
                line = item
            else:
                line = parsed_item.get("stream", "")
            line = line.replace("\r\n", " ").replace("\n", " ").strip()
            if line:
                logger.debug(line)
            logs.append(item)
            if parsed_item is not None:
                error = parsed_item.get("error", None)
                error_message = parsed_item.get("errorDetail", None)
                if error:
                    logger.error(item.strip())
                    raise RuntimeError("Error in container processing: %s (%s)" % (error, error_message))
        except StopIteration:
            logger.info("no more logs")
            break
    return logs


def clone_git_repo(git_url, target_dir, commit=None):
    """
    clone provided git repo to target_dir, optionally checkout provided commit

    :param git_url: str, git repo to clone
    :param target_dir: str, filesystem path where the repo should be cloned
    :param commit: str, commit to checkout
    :return:
    """
    logger.info("clone git repo")
    logger.debug("url = '%s', dir = '%s', commit = '%s'",
                 git_url, target_dir, commit)
    repo = git.Repo.clone_from(git_url, target_dir)
    if commit:
        repo.git.checkout(commit)


def figure_out_dockerfile(absolute_path, local_path=None):
    """
    try to figure out dockerfile from provided path and optionally from relative local path
    this is meant to be used with git repo: absolute_path is path to git repo,
    local_path is path to dockerfile within git repo

    :param absolute_path:
    :param local_path:
    :return: tuple, (dockerfile_path, dir_with_dockerfile_path)
    """
    logger.info("find dockerfile")
    logger.debug("abs path = '%s', local path = '%s'", absolute_path, local_path)
    if local_path:
        if local_path.endswith(DOCKERFILE_FILENAME):
            git_df_dir = os.path.dirname(local_path)
            df_dir = os.path.abspath(os.path.join(absolute_path, git_df_dir))
        else:
            df_dir = os.path.abspath(os.path.join(absolute_path, local_path))
    else:
        df_dir = os.path.abspath(absolute_path)
    if not os.path.isdir(df_dir):
        raise IOError("Directory '%s' doesn't exist." % df_dir)
    df_path = os.path.join(df_dir, DOCKERFILE_FILENAME)
    if not os.path.isfile(df_path):
        raise IOError("Dockerfile '%s' doesn't exist." % df_path)
    logger.debug("dockerfile found: '%s'", df_path)
    return df_path, df_dir


class LazyGit(object):
    """
    usage:

        lazy_git = LazyGit(git_url="...")
        with lazy_git:
            laze_git.git_path

    or

        lazy_git = LazyGit(git_url="...", tmpdir=tmp_dir)
        lazy_git.git_path
    """
    def __init__(self, git_url, commit=None, tmpdir=None):
        self.git_url = git_url
        self.commit = commit
        self.provided_tmpdir = tmpdir
        self._git_path = None

    @property
    def _tmpdir(self):
        return self.provided_tmpdir or self.our_tmpdir

    @property
    def git_path(self):
        if self._git_path is None:
            clone_git_repo(self.git_url, self._tmpdir, self.commit)
            self._git_path = self._tmpdir
        return self._git_path

    def __enter__(self):
        if not self.provided_tmpdir:
            self.our_tmpdir = tempfile.mkdtemp()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.provided_tmpdir:
            if self.our_tmpdir:
                shutil.rmtree(self.our_tmpdir)
