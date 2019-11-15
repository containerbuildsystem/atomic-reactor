"""
Copyright (c) 2015, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Script for building docker image. This is expected to run inside container.
"""

from __future__ import absolute_import, division

import json
import logging
import tempfile
import signal
import threading
import os
import time

from atomic_reactor.build import InsideBuilder
from atomic_reactor.plugin import (
    AutoRebuildCanceledException,
    BuildCanceledException,
    BuildStepPluginsRunner,
    ExitPluginsRunner,
    InputPluginsRunner,
    PluginFailedException,
    PostBuildPluginsRunner,
    PreBuildPluginsRunner,
    PrePublishPluginsRunner,
)
from atomic_reactor.source import get_source_instance_for, DummySource
from atomic_reactor.constants import INSPECT_ROOTFS, INSPECT_ROOTFS_LAYERS
from atomic_reactor.constants import (
    CONTAINER_DEFAULT_BUILD_METHOD,
    PLUGIN_BUILD_ORCHESTRATE_KEY
)
from atomic_reactor.util import ImageName, exception_message
from atomic_reactor.build import BuildResult
from atomic_reactor import get_logging_encoding


logger = logging.getLogger(__name__)


class BuildResults(object):
    build_logs = None
    dockerfile = None
    built_img_inspect = None
    built_img_info = None
    base_img_inspect = None
    base_img_info = None
    base_plugins_output = None
    built_img_plugins_output = None
    container_id = None
    return_code = None


class BuildResultsEncoder(json.JSONEncoder):
    def default(self, obj):  # pylint: disable=method-hidden
        if isinstance(obj, BuildResults):
            return {
                'build_logs': obj.build_logs,
                'built_img_inspect': obj.built_img_inspect,
                'built_img_info': obj.built_img_info,
                'base_img_info': obj.base_img_info,
                'base_plugins_output': obj.base_plugins_output,
                'built_img_plugins_output': obj.built_img_plugins_output,
            }
        # Let the base class default method raise the TypeError
        return json.JSONEncoder.default(self, obj)


class BuildResultsJSONDecoder(json.JSONDecoder):
    def decode(self, obj):
        d = super(BuildResultsJSONDecoder, self).decode(obj)
        results = BuildResults()
        results.built_img_inspect = d.get('built_img_inspect', None)
        results.built_img_info = d.get('built_img_info', None)
        results.base_img_info = d.get('base_img_info', None)
        results.base_plugins_output = d.get('base_plugins_output', None)
        results.built_img_plugins_output = d.get('built_img_plugins_output', None)
        return results


class TagConf(object):
    """
    confguration of image names and tags to be applied
    """

    def __init__(self):
        # list of ImageNames with 'static' tags
        self._primary_images = []
        # list if ImageName instances with unpredictable names
        self._unique_images = []
        # list of ImageName instances with 'floating' tags
        # which can be updated by other images later
        self._floating_images = []

    @property
    def primary_images(self):
        """
        primary image names are static and should be used for layering

        this is consumed by metadata plugin

        :return: list of ImageName
        """
        return self._primary_images

    @property
    def images(self):
        """
        list of all ImageNames

        :return: list of ImageName
        """
        return self._primary_images + self._unique_images + self._floating_images

    @property
    def unique_images(self):
        """
        unique image names are unpredictable and should be used for tracking only

        this is consumed by metadata plugin

        :return: list of ImageName
        """
        return self._unique_images

    @property
    def floating_images(self):
        """
        floating image names are floating and should be used for layering

        this is consumed by metadata plugin

        :return: list of ImageName
        """
        return self._floating_images

    def add_primary_image(self, image):
        """
        add new primary image

        used by tag_from_config plugin

        :param image: str, name of image (e.g. "namespace/httpd:2.4")
        :return: None
        """
        self._primary_images.append(ImageName.parse(image))

    def add_unique_image(self, image):
        """
        add image with unpredictable name

        used by tag_from_config plugin

        :param image: str, name of image (e.g. "namespace/httpd:2.4")
        :return: None
        """
        self._unique_images.append(ImageName.parse(image))

    def add_floating_image(self, image):
        """
        add image with floating name

        used by tag_from_config plugin

        :param image: str, name of image (e.g. "namespace/httpd:2.4")
        :return: None
        """
        self._floating_images.append(ImageName.parse(image))

    def add_primary_images(self, images):
        """
        add new primary images in bulk

        used by tag_from_config plugin

        :param images: list of str, list of image names
        :return: None
        """
        for image in images:
            self.add_primary_image(image)

    def add_floating_images(self, images):
        """
        add new floating images in bulk

        used by tag_from_config plugin

        :param images: list of str, list of image names
        :return: None
        """
        for image in images:
            self.add_floating_image(image)


class Registry(object):
    def __init__(self, uri, insecure=False):
        """
        abstraction for all registry classes

        :param uri: str, uri for pulling (in case of docker-registry, pushing too)
        :param insecure: bool
        """
        self.uri = uri
        self.insecure = insecure


class DockerRegistry(Registry):
    """ v2 docker registry """
    def __init__(self, uri, insecure=False):
        """
        :param uri: str, uri for pushing/pulling
        :param insecure: bool
        """
        super(DockerRegistry, self).__init__(uri, insecure=insecure)
        self.digests = {}  # maps a tag (str) to a ManifestDigest instance, if available
        self.config = None  # stores image config from the registry,
        # media type of the config is application/vnd.docker.container.image.v1+json


class PushConf(object):
    """
    configuration of remote registries: docker-registry
    """

    def __init__(self):
        self._registries = {
            "docker": [],
        }

    def add_docker_registry(self, registry_uri, insecure=False):
        if registry_uri is None:
            raise RuntimeError("registry URI cannot be None")
        r = DockerRegistry(registry_uri, insecure=insecure)
        self._registries["docker"].append(r)
        return r

    def remove_docker_registry(self, docker_registry):
        self._registries["docker"].remove(docker_registry)

    @property
    def has_some_docker_registry(self):
        return len(self.docker_registries) > 0

    @property
    def docker_registries(self):
        return self._registries["docker"]

    @property
    def all_registries(self):
        return self.docker_registries


class FSWatcher(threading.Thread):
    """
    Poll the filesystem every second in the background and keep a record of highest usage.
    """

    def __init__(self, *args, **kwargs):
        super(FSWatcher, self).__init__(*args, **kwargs)
        self.daemon = True  # exits whenever the process exits
        self._lock = threading.Lock()
        self._done = False
        self._data = {}

    def run(self):
        """ Overrides parent method to implement thread's functionality. """
        while True:  # make sure to run at least once before exiting
            with self._lock:
                self._update(self._data)
            if self._done:
                break
            time.sleep(1)

    def get_usage_data(self):
        """ Safely retrieve the most up to date results. """
        with self._lock:
            data_copy = self._data.copy()
        return data_copy

    def finish(self):
        """ Signal background thread to exit next time it wakes up. """
        with self._lock:  # just to be tidy; lock not really needed to set a boolean
            self._done = True

    @staticmethod
    def _update(data):
        try:
            st = os.statvfs("/")
        except Exception as e:
            return e  # just for tests; we don't really need return value

        mb = 1000 ** 2  # sadly storage is generally expressed in decimal units
        new_data = dict(
            mb_free=st.f_bfree * st.f_frsize // mb,
            mb_total=st.f_blocks * st.f_frsize // mb,
            mb_used=(st.f_blocks - st.f_bfree) * st.f_frsize // mb,
            inodes_free=st.f_ffree,
            inodes_total=st.f_files,
            inodes_used=st.f_files - st.f_ffree,
        )
        for key in ["mb_total", "mb_used", "inodes_total", "inodes_used"]:
            data[key] = max(new_data[key], data.get(key, 0))
        for key in ["mb_free", "inodes_free"]:
            data[key] = min(new_data[key], data.get(key, float("inf")))

        return new_data


class DockerBuildWorkflow(object):
    """
    This class defines a workflow for building images:

    1. pull image from registry
    2. tag it properly if needed
    3. obtain source
    4. build image
    5. tag it
    6. push it to registries
    """

    def __init__(self, image, source=None, prebuild_plugins=None, prepublish_plugins=None,
                 postbuild_plugins=None, exit_plugins=None, plugin_files=None,
                 openshift_build_selflink=None, client_version=None,
                 buildstep_plugins=None, **kwargs):
        """
        :param source: dict, where/how to get source code to put in image
        :param image: str, tag for built image ([registry/]image_name[:tag])
        :param prebuild_plugins: list of dicts, arguments for pre-build plugins
        :param prepublish_plugins: list of dicts, arguments for test-build plugins
        :param postbuild_plugins: list of dicts, arguments for post-build plugins
        :param exit_plugins: list of dicts, arguments for exit plugins
        :param plugin_files: list of str, load plugins also from these files
        :param openshift_build_selflink: str, link to openshift build (if we're actually running
            on openshift) without the actual hostname/IP address
        :param client_version: str, osbs-client version used to render build json
        :param buildstep_plugins: list of dicts, arguments for build-step plugins
        """
        tmp_dir = tempfile.mkdtemp()
        if source is None:
            self.source = DummySource(None, None, tmpdir=tmp_dir)
        else:
            self.source = get_source_instance_for(source, tmpdir=tmp_dir)
        self.image = image

        self.prebuild_plugins_conf = prebuild_plugins
        self.buildstep_plugins_conf = buildstep_plugins
        self.prepublish_plugins_conf = prepublish_plugins
        self.postbuild_plugins_conf = postbuild_plugins
        self.exit_plugins_conf = exit_plugins
        self.prebuild_results = {}
        self.buildstep_result = {}
        self.postbuild_results = {}
        self.prepub_results = {}
        self.exit_results = {}
        self.build_result = BuildResult(fail_reason="not built")
        self.plugin_workspace = {}
        self.plugins_timestamps = {}
        self.plugins_durations = {}
        self.plugins_errors = {}
        self.autorebuild_canceled = False
        self.build_canceled = False
        self.plugin_failed = False
        self.plugin_files = plugin_files
        self.fs_watcher = FSWatcher()

        self.kwargs = kwargs

        self.builder = None
        self.built_image_inspect = None
        self.layer_sizes = []
        self.default_image_build_method = CONTAINER_DEFAULT_BUILD_METHOD

        # list of images pulled during the build, to be deleted after the build
        self.pulled_base_images = set()

        # When an image is exported into tarball, it can then be processed by various plugins.
        #  Each plugin that transforms the image should save it as a new file and append it to
        #  the end of exported_image_sequence. Other plugins should then operate with last
        #  member of this structure. Example:
        #  [{'path': '/tmp/foo.tar', 'size': 12345678, 'md5sum': '<md5>', 'sha256sum': '<sha256>'}]
        #  You can use util.get_exported_image_metadata to create a dict to append to this list.
        self.exported_image_sequence = []

        self.tag_conf = TagConf()
        self.push_conf = PushConf()

        # mapping of downloaded files; DON'T PUT ANYTHING BIG HERE!
        # "path/to/file" -> "content"
        self.files = {}

        self.openshift_build_selflink = openshift_build_selflink

        # List of RPMs that go into the final result, as per rpm_util.parse_rpm_output
        self.image_components = None

        # List of all yum repos. The provided repourls might be changed (by resolve_composes) when
        # inheritance is enabled. This property holds the updated list of repos, allowing
        # post-build plugins (such as koji_import) to record them.
        self.all_yum_repourls = None

        # info about pre-declared build, build-id and token
        self.reserved_build_id = None
        self.reserved_token = None
        self.triggered_after_koji_task = None
        self.koji_source_nvr = {}
        self.koji_source_source_url = None

        if client_version:
            logger.debug("build json was built by osbs-client %s", client_version)

        if kwargs:
            logger.warning("unprocessed keyword arguments: %s", kwargs)

    def get_orchestrate_build_plugin(self):
        """
        Get the orchestrate_build plugin configuration for this workflow
        if present (will be present for orchestrator, not for worker).

        :return: orchestrate_build plugin configuration dict
        :raises: ValueError if the orchestrate_build plugin is not present
        """
        for plugin in self.buildstep_plugins_conf or []:
            if plugin['name'] == PLUGIN_BUILD_ORCHESTRATE_KEY:
                return plugin
        # Not an orchestrator build
        raise ValueError('Not an orchestrator build')

    def is_orchestrator_build(self):
        """
        Check if the plugin configuration for this workflow is for
        an orchestrator build or a worker build.

        :return: True if orchestrator build, False if worker build
        """
        try:
            self.get_orchestrate_build_plugin()
            return True
        except ValueError:
            return False

    @property
    def build_process_failed(self):
        """
        Has any aspect of the build process failed?
        """
        return self.build_result.is_failed() or self.plugin_failed

    def throw_canceled_build_exception(self, *args, **kwargs):
        self.build_canceled = True
        raise BuildCanceledException("Build was canceled")

    def build_docker_image(self):
        """
        build docker image

        :return: BuildResult
        """
        exception_being_handled = False
        self.builder = InsideBuilder(self.source, self.image)
        # Make sure exit_runner is defined for finally block
        exit_runner = None
        try:
            self.fs_watcher.start()
            signal.signal(signal.SIGTERM, self.throw_canceled_build_exception)
            prebuild_runner = PreBuildPluginsRunner(self.builder.tasker, self,
                                                    self.prebuild_plugins_conf,
                                                    plugin_files=self.plugin_files)
            prepublish_runner = PrePublishPluginsRunner(self.builder.tasker, self,
                                                        self.prepublish_plugins_conf,
                                                        plugin_files=self.plugin_files)
            postbuild_runner = PostBuildPluginsRunner(self.builder.tasker, self,
                                                      self.postbuild_plugins_conf,
                                                      plugin_files=self.plugin_files)
            # time to run pre-build plugins, so they can access cloned repo
            logger.info("running pre-build plugins")
            try:
                prebuild_runner.run()
            except PluginFailedException as ex:
                logger.error("one or more prebuild plugins failed: %s", ex)
                raise
            except AutoRebuildCanceledException as ex:
                logger.info(str(ex))
                self.autorebuild_canceled = True
                raise

            # we are delaying initialization, because prebuild plugin reactor_config
            # might change build method
            buildstep_runner = BuildStepPluginsRunner(self.builder.tasker, self,
                                                      self.buildstep_plugins_conf,
                                                      plugin_files=self.plugin_files)

            logger.info("running buildstep plugins")
            try:
                self.build_result = buildstep_runner.run()

                if self.build_result.is_failed():
                    raise PluginFailedException(self.build_result.fail_reason)
            except PluginFailedException as ex:
                self.builder.is_built = False
                logger.error('buildstep plugin failed: %s', ex)
                raise

            self.builder.is_built = True
            if self.build_result.is_image_available():
                self.builder.image_id = self.build_result.image_id

            # run prepublish plugins
            try:
                prepublish_runner.run()
            except PluginFailedException as ex:
                logger.error("one or more prepublish plugins failed: %s", ex)
                raise

            if self.build_result.is_image_available():
                self.built_image_inspect = self.builder.inspect_built_image()
                history = self.builder.tasker.get_image_history(self.builder.image_id)
                diff_ids = self.built_image_inspect[INSPECT_ROOTFS][INSPECT_ROOTFS_LAYERS]

                # diff_ids is ordered oldest first
                # history is ordered newest first
                # We want layer_sizes to be ordered oldest first
                self.layer_sizes = [{"diff_id": diff_id, "size": layer['Size']}
                                    for (diff_id, layer) in zip(diff_ids, reversed(history))]

            try:
                postbuild_runner.run()
            except PluginFailedException as ex:
                logger.error("one or more postbuild plugins failed: %s", ex)
                raise

            return self.build_result
        except Exception as ex:
            logger.debug("caught exception (%s) so running exit plugins", exception_message(ex))
            exception_being_handled = True
            raise
        finally:
            # We need to make sure all exit plugins are executed
            signal.signal(signal.SIGTERM, lambda *args: None)

            exit_runner = ExitPluginsRunner(self.builder.tasker, self,
                                            self.exit_plugins_conf,
                                            keep_going=True,
                                            plugin_files=self.plugin_files)
            try:
                exit_runner.run(keep_going=True)
            except PluginFailedException as ex:
                logger.error("one or more exit plugins failed: %s", ex)

                # raise exception only in case that there is no previous exception being already
                # handled to prevent replacing original exceptions (root cause) with exceptions
                # from exit plugins
                if not exception_being_handled:
                    raise ex
            finally:
                self.source.remove_tmpdir()
                self.fs_watcher.finish()

            signal.signal(signal.SIGTERM, signal.SIG_DFL)


def build_inside(input_method, input_args=None, substitutions=None):
    """
    use requested input plugin to load configuration and then initiate build
    """
    def process_keyvals(keyvals):
        """ ["key=val", "x=y"] -> {"key": "val", "x": "y"} """
        keyvals = keyvals or []
        processed_keyvals = {}
        for arg in keyvals:
            key, value = arg.split("=", 1)
            processed_keyvals[key] = value
        return processed_keyvals

    main = __name__.split('.', 1)[0]
    log_encoding = get_logging_encoding(main)
    logger.info("log encoding: %s", log_encoding)

    if not input_method:
        raise RuntimeError("No input method specified!")

    logger.debug("getting build json from input %s", input_method)

    cleaned_input_args = process_keyvals(input_args)
    cleaned_input_args['substitutions'] = process_keyvals(substitutions)

    input_runner = InputPluginsRunner([{'name': input_method,
                                        'args': cleaned_input_args}])
    build_json = input_runner.run()[input_method]

    if isinstance(build_json, Exception):
        raise RuntimeError("Input plugin raised exception: {}".format(build_json))
    logger.debug("build json: %s", build_json)
    if not build_json:
        raise RuntimeError("No valid build json!")
    if not isinstance(build_json, dict):
        raise RuntimeError("Input plugin did not return valid build json: {}".format(build_json))

    dbw = DockerBuildWorkflow(**build_json)
    try:
        build_result = dbw.build_docker_image()
    except Exception as e:
        logger.info("Dockerfile used for build:\n%s", dbw.builder.original_df)
        logger.error('image build failed: %s', e)
        raise
    else:
        logger.info("Dockerfile used for build:\n%s", dbw.builder.original_df)
        if not build_result or build_result.is_failed():
            raise RuntimeError("no image built")
        else:
            logger.info("build has finished successfully \\o/")
