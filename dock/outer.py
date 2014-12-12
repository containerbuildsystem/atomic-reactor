"""

"""
from functools import partial
import json
import os
import shutil
import tempfile
import datetime
import logging

from dock.constants import BUILD_JSON, RESULTS_JSON
from dock.build import BuilderStateMachine
from dock.core import DockerTasker, BuildContainerWarlock
from dock.inner import BuildResultsJSONDecoder


logger = logging.getLogger(__name__)


class BuildManager(BuilderStateMachine):
    """
    initiates build and waits for it to finish, then it collects data
    """
    def __init__(self, build_image, build_args):
        BuilderStateMachine.__init__(self)
        self.build_image = build_image
        self.build_args = build_args
        self.image = build_args['image']
        self.git_url = build_args['git_url']

        self.temp_dir = None
        # build image after build
        self.buildroot_image_id = None
        self.buildroot_image_tag = None
        self.buildroot_image_name = None
        self.dt = DockerTasker()

    def _build(self, build_method):
        """
        build image from provided build_args

        :return: BuildResults
        """
        logger.info("build image")
        self._ensure_not_built()
        self.temp_dir = tempfile.mkdtemp()
        temp_path = os.path.join(self.temp_dir, BUILD_JSON)
        try:
            with open(temp_path, 'w') as build_json:
                json.dump(self.build_args, build_json)
            self.build_container_id = build_method(self.build_image, self.temp_dir)
            self.dt.wait(self.build_container_id)
            self.is_built = True
            return self._load_results(self.build_container_id)
        finally:
            shutil.rmtree(self.temp_dir)

    def _load_results(self, container_id):
        """
        load results from recent build

        :return: BuildResults
        """
        if self.temp_dir:
            dt = DockerTasker()
            results_path = os.path.join(self.temp_dir, RESULTS_JSON)
            #df_path = os.path.join(self.temp_dir, 'Dockerfile')
            try:
                with open(results_path, 'r') as results_fp:
                    results = json.load(results_fp, cls=BuildResultsJSONDecoder)
            except (IOError, OSError) as ex:
                logger.error("Can't open results: '%s'", repr(ex))
                for l in self.dt.logs(self.build_container_id, stream=False):
                    logger.debug(l.strip())
                raise RuntimeError("Can't open results: '%s'" % repr(ex))
            #results.dockerfile = open(df_path, 'r').read()
            results.build_logs = dt.logs(container_id, stream=False)
            results.container_id = container_id
            return results

    def commit_buildroot(self):
        """
        create image from buildroot

        :return:
        """
        logger.info("commit buildroot")
        self._ensure_is_built()
        # save the time when image was built
        self.buildroot_image_tag = datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')

        commit_message = "docker build of '%s' (%s)" % (self.image, self.git_url)
        self.buildroot_image_name = "buildroot-%s" % self.image
        self.buildroot_image_id = self.dt.commit_container(self.build_container_id, commit_message)
        return self.buildroot_image_id

    def push_buildroot(self, registry):
        logger.info("push buildroot to registry")
        self._ensure_is_built()
        return self.dt.tag_and_push_image(
            self.buildroot_image_id,
            self.buildroot_image_name,
            reg_uri=registry,
            tag=self.buildroot_image_tag)


class PrivilegedBuildManager(BuildManager):
    def build(self):
        w = BuildContainerWarlock()
        return super(PrivilegedBuildManager, self)._build(
            partial(BuildContainerWarlock.build_image_privileged_container, w))


class DockerhostBuildManager(BuildManager):
    def build(self):
        w = BuildContainerWarlock()
        return super(DockerhostBuildManager, self)._build(
            partial(BuildContainerWarlock.build_image_dockerhost, w))
