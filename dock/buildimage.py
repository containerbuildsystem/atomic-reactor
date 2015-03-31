"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


supported use cases:

 * build image from remote git (without specifying address)
 * build image from local git repo
 * build image using provided tarball (made with sdist)
"""
import os
import shutil
import subprocess
import tempfile
import logging
from glob import glob
import uuid

from dock.core import DockerTasker
from dock.util import LazyGit, wait_for_command, ImageName


logger = logging.getLogger(__name__)


DOCK_GIT_URL = "https://github.com/DBuildService/dock.git"
DOCKERFILE_DOCK_TARBALL_NAME = "dock.tar.gz"


class BuildImageBuilder(object):
    def __init__(self, dock_tarball_path=None, dock_local_path=None,
                 dock_remote_path=None, use_official_dock_git=False):
        self.tasker = DockerTasker()
        self.dock_tarball_path = dock_tarball_path
        self.dock_local_path = dock_local_path
        self.dock_remote_path = dock_remote_path
        self.use_official_dock_git = use_official_dock_git
        if not self.dock_tarball_path and \
           not self.dock_local_path and \
           not self.dock_remote_path and \
           not self.use_official_dock_git:
            logger.error("no dock source specified, can't proceed")
            raise RuntimeError("You have to specify dock source: either local gitrepo, "
                               "path to dock tarball, or use upstream git repo.")

    def create_image(self, df_dir_path, image, use_cache=False):
        """
        create image: get dock sdist tarball, build image and tag it

        :param df_path:
        :param image:
        :return:
        """
        logger.debug("df_dir_path = '%s', image = '%s'", df_dir_path, image)

        if not os.path.isdir(df_dir_path):
            raise RuntimeError("Directory '%s' does not exist.", df_dir_path)

        tmpdir = tempfile.mkdtemp()
        df_tmpdir = os.path.join(tmpdir, 'df-%s' % uuid.uuid4())
        git_tmpdir = os.path.join(tmpdir, 'git-%s' % uuid.uuid4())
        os.mkdir(df_tmpdir)
        logger.debug("tmp dir with dockerfile '%s' created", df_tmpdir)
        os.mkdir(git_tmpdir)
        logger.debug("tmp dir with dock '%s' created", git_tmpdir)
        try:
            for f in glob(os.path.join(df_dir_path, '*')):
                shutil.copy(f, df_tmpdir)
                logger.debug("cp '%s' -> '%s'", f, df_tmpdir)
            logger.debug("df dir: %s", os.listdir(df_tmpdir))
            dock_tarball = self.get_dock_tarball_path(tmpdir=git_tmpdir)
            dock_tb_path = os.path.join(df_tmpdir, DOCKERFILE_DOCK_TARBALL_NAME)
            shutil.copy(dock_tarball, dock_tb_path)

            image_name = ImageName.parse(image)
            logs_gen = self.tasker.build_image_from_path(df_tmpdir, image_name, stream=True, use_cache=use_cache)
            wait_for_command(logs_gen)
        finally:
            shutil.rmtree(tmpdir)

    def get_dock_tarball_path(self, tmpdir):
        """
        generate dock tarball
        :return:
        """
        if self.dock_tarball_path:
            if not os.path.isfile(self.dock_tarball_path):
                logger.error("dock sdist tarball does not exist: '%s'", self.dock_tarball_path)
                raise RuntimeError("File does not exist: '%s'" % self.dock_tarball_path)
            return self.dock_tarball_path
        elif self.dock_local_path:
            if not os.path.isdir(self.dock_local_path):
                logger.error("local dock git clone does not exist: '%s'", self.dock_local_path)
                raise RuntimeError("Local dock git repo does not exist: '%s'" % self.dock_local_path)
            local_dock_git_path = self.dock_local_path
        else:
            if self.use_official_dock_git:
                self.dock_remote_path = DOCK_GIT_URL

            g = LazyGit(self.dock_remote_path, tmpdir=tmpdir)
            local_dock_git_path = g.git_path

        cwd = os.getcwd()
        os.chdir(local_dock_git_path)
        try:
            logger.debug("executing sdist command in directory '%s'", os.getcwd())
            subprocess.check_call(["python", "setup.py", "sdist", "--dist-dir", tmpdir])
        finally:
            os.chdir(cwd)
        candidates_list = glob(os.path.join(tmpdir, 'dock-*.tar.gz'))
        if len(candidates_list) == 1:
            return candidates_list[0]
        else:
            logger.warning("len(dock-*.tar.gz) != 1: '%s'", candidates_list)
            try:
                return candidates_list[0]
            except IndexError:
                raise RuntimeError("No dock tarball built.")
