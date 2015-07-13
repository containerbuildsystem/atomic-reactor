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

from atomic_reactor.core import DockerTasker
from atomic_reactor.util import LazyGit, wait_for_command, ImageName


logger = logging.getLogger(__name__)


REACTOR_GIT_URL = "https://github.com/projectatomic/atomic-reactor.git"
DOCKERFILE_REACTOR_TARBALL_NAME = "atomic-reactor.tar.gz"


class BuildImageBuilder(object):
    def __init__(self, reactor_tarball_path=None, reactor_local_path=None,
                 reactor_remote_path=None, use_official_reactor_git=False):
        self.tasker = DockerTasker()
        self.reactor_tarball_path = reactor_tarball_path
        self.reactor_local_path = reactor_local_path
        self.reactor_remote_path = reactor_remote_path
        self.use_official_reactor_git = use_official_reactor_git
        if not self.reactor_tarball_path and \
           not self.reactor_local_path and \
           not self.reactor_remote_path and \
           not self.use_official_reactor_git:
            logger.error("no atomic_reactor source specified, can't proceed")
            raise RuntimeError("You have to specify atomic_reactor source: either local gitrepo, "
                               "path to atomic_reactor tarball, or use upstream git repo.")

    def create_image(self, df_dir_path, image, use_cache=False):
        """
        create image: get atomic-reactor sdist tarball, build image and tag it

        :param df_path:
        :param image:
        :return:
        """
        logger.debug("creating build image: df_dir_path = '%s', image = '%s'", df_dir_path, image)

        if not os.path.isdir(df_dir_path):
            raise RuntimeError("Directory '%s' does not exist.", df_dir_path)

        tmpdir = tempfile.mkdtemp()
        df_tmpdir = os.path.join(tmpdir, 'df-%s' % uuid.uuid4())
        git_tmpdir = os.path.join(tmpdir, 'git-%s' % uuid.uuid4())
        os.mkdir(df_tmpdir)
        logger.debug("tmp dir with dockerfile '%s' created", df_tmpdir)
        os.mkdir(git_tmpdir)
        logger.debug("tmp dir with atomic-reactor '%s' created", git_tmpdir)
        try:
            for f in glob(os.path.join(df_dir_path, '*')):
                shutil.copy(f, df_tmpdir)
                logger.debug("cp '%s' -> '%s'", f, df_tmpdir)
            logger.debug("df dir: %s", os.listdir(df_tmpdir))
            reactor_tarball = self.get_reactor_tarball_path(tmpdir=git_tmpdir)
            reactor_tb_path = os.path.join(df_tmpdir, DOCKERFILE_REACTOR_TARBALL_NAME)
            shutil.copy(reactor_tarball, reactor_tb_path)

            image_name = ImageName.parse(image)
            logs_gen = self.tasker.build_image_from_path(df_tmpdir, image_name, stream=True, use_cache=use_cache)
            wait_for_command(logs_gen)
        finally:
            shutil.rmtree(tmpdir)

    def get_reactor_tarball_path(self, tmpdir):
        """
        generate atomic-reactor tarball
        :return:
        """
        if self.reactor_tarball_path:
            if not os.path.isfile(self.reactor_tarball_path):
                logger.error("atomic-reactor sdist tarball does not exist: '%s'", self.reactor_tarball_path)
                raise RuntimeError("File does not exist: '%s'" % self.reactor_tarball_path)
            return self.reactor_tarball_path
        elif self.reactor_local_path:
            if not os.path.isdir(self.reactor_local_path):
                logger.error("local atomic-reactor git clone does not exist: '%s'", self.reactor_local_path)
                raise RuntimeError("Local atomic-reactor git repo does not exist: '%s'" % self.reactor_local_path)
            local_reactor_git_path = self.reactor_local_path
        else:
            if self.use_official_reactor_git:
                self.reactor_remote_path = REACTOR_GIT_URL

            g = LazyGit(self.reactor_remote_path, tmpdir=tmpdir)
            local_reactor_git_path = g.git_path

        cwd = os.getcwd()
        os.chdir(local_reactor_git_path)
        try:
            logger.debug("executing sdist command in directory '%s'", os.getcwd())
            subprocess.check_call(["python", "setup.py", "sdist", "--dist-dir", tmpdir])
        finally:
            os.chdir(cwd)
        candidates_list = glob(os.path.join(tmpdir, 'atomic-reactor-*.tar.gz'))
        if len(candidates_list) == 1:
            return candidates_list[0]
        else:
            logger.warning("len(atomic-reactor-*.tar.gz) != 1: '%s'", candidates_list)
            try:
                return candidates_list[0]
            except IndexError:
                raise RuntimeError("No atomic-reactor tarball built.")
