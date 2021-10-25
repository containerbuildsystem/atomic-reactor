"""
Copyright (c) 2015, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Script for building docker image. This is expected to run inside container.
"""

import json
import logging
import signal
import threading
import os
import time
import re
from textwrap import dedent
from typing import List

from atomic_reactor.plugin import (
    BuildCanceledException,
    BuildStepPluginsRunner,
    ExitPluginsRunner,
    PluginFailedException,
    PostBuildPluginsRunner,
    PreBuildPluginsRunner,
    PrePublishPluginsRunner,
)
from atomic_reactor.constants import (
    DOCKER_STORAGE_TRANSPORT_NAME,
    INSPECT_ROOTFS,
    INSPECT_ROOTFS_LAYERS,
    PLUGIN_BUILD_ORCHESTRATE_KEY,
    REACTOR_CONFIG_FULL_PATH,
    DOCKERFILE_FILENAME,
)
from atomic_reactor.util import (exception_message, DockerfileImages, df_parser,
                                 base_image_is_custom, print_version_of_tools)
from atomic_reactor.config import Configuration
from atomic_reactor.source import Source, DummySource
from atomic_reactor.tasks import PluginsDef
from atomic_reactor.utils import imageutil
# from atomic_reactor import get_logging_encoding
from osbs.utils import ImageName


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
    def default(self, obj):  # pylint: disable=method-hidden,arguments-renamed
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


class BuildResult(object):

    REMOTE_IMAGE = object()

    def __init__(self, logs=None, fail_reason=None, image_id=None,
                 annotations=None, labels=None, skip_layer_squash=False,
                 source_docker_archive=None):
        """
        :param logs: iterable of log lines (without newlines)
        :param fail_reason: str, description of failure or None if successful
        :param image_id: str, ID of built container image
        :param annotations: dict, data captured during build step which
                            should be annotated to OpenShift build
        :param labels: dict, data captured during build step which
                       should be set as labels on OpenShift build
        :param skip_layer_squash: boolean, direct post-build plugins not
                                  to squash image layers for this build
        :param source_docker_archive: str, path to docker image archive
        """
        assert fail_reason is None or bool(fail_reason), \
            "If fail_reason provided, can't be falsy"
        # must provide one, not both
        assert not (fail_reason and image_id), \
            "Either fail_reason or image_id should be provided, not both"
        assert not (fail_reason and source_docker_archive), \
            "Either fail_reason or source_docker_archive should be provided, not both"
        assert not (image_id and source_docker_archive), \
            "Either image_id or source_docker_archive should be provided, not both"
        self._logs = logs or []
        self._fail_reason = fail_reason
        self._image_id = image_id
        self._annotations = annotations
        self._labels = labels
        self._skip_layer_squash = skip_layer_squash
        self._source_docker_archive = source_docker_archive

    @classmethod
    def make_remote_image_result(cls, annotations=None, labels=None):
        """Instantiate BuildResult for image not built locally."""
        return cls(
            image_id=cls.REMOTE_IMAGE, annotations=annotations, labels=labels
        )

    @property
    def logs(self):
        return self._logs

    @property
    def fail_reason(self):
        return self._fail_reason

    def is_failed(self):
        return self._fail_reason is not None

    @property
    def image_id(self):
        return self._image_id

    @property
    def annotations(self):
        return self._annotations

    @property
    def labels(self):
        return self._labels

    @property
    def skip_layer_squash(self):
        return self._skip_layer_squash

    @property
    def source_docker_archive(self):
        return self._source_docker_archive

    def is_image_available(self):
        return self._image_id and self._image_id is not self.REMOTE_IMAGE


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
            "docker": {},  # URI -> DockerRegistry instance
        }

    def add_docker_registry(self, registry_uri, insecure=False):
        if registry_uri is None:
            raise RuntimeError("registry URI cannot be None")
        try:
            return self._registries["docker"][registry_uri]
        except KeyError:
            r = DockerRegistry(registry_uri, insecure=insecure)
            self._registries["docker"][registry_uri] = r

        return r

    def remove_docker_registry(self, docker_registry):
        for uri, registry in self._registries["docker"].items():
            if registry == docker_registry:
                del self._registries["docker"][uri]
                return

    @property
    def has_some_docker_registry(self):
        return bool(self.docker_registries)

    @property
    def docker_registries(self):
        return list(self._registries["docker"].values())

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

    # The only reason this is here is to have something that unit tests can monkeypatch
    _default_user_params = {}

    def __init__(
        self,
        source: Source = None,
        plugins: PluginsDef = None,
        user_params: dict = None,
        reactor_config_path: str = REACTOR_CONFIG_FULL_PATH,
        plugin_files: List[str] = None,
        client_version: str = None,
    ):
        """
        :param source: where/how to get source code to put in image
        :param plugins: the plugins to be executed in this workflow
        :param user_params: user (and other) params that control various aspects of the build
        :param reactor_config_path: path to atomic-reactor configuration file
        :param plugin_files: load plugins also from these files
        :param client_version: osbs-client version used to render build json
        """
        self.source = source or DummySource(None, None)
        self.plugins = plugins or PluginsDef()
        self.user_params = user_params or self._default_user_params.copy()

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
        self.build_canceled = False
        self.plugin_failed = False
        self.plugin_files = plugin_files
        self.fs_watcher = FSWatcher()

        self.built_image_inspect = None
        self.layer_sizes = []
        self.storage_transport = DOCKER_STORAGE_TRANSPORT_NAME

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

        # List of RPMs that go into the final result, as per utils.rpm.parse_rpm_output
        self.image_components = None

        # List of all yum repos. The provided repourls might be changed (by resolve_composes) when
        # inheritance is enabled. This property holds the updated list of repos, allowing
        # post-build plugins (such as koji_import) to record them.
        self.all_yum_repourls = None

        # info about pre-declared build, build-id and token
        self.reserved_build_id = None
        self.reserved_token = None
        self.cancel_isolated_autorebuild = False
        self.koji_source_nvr = {}
        self.koji_source_source_url = None
        self.koji_source_manifest = None

        # Plugins can store info here using the @annotation, @annotation_map,
        # @label and @label_map decorators from atomic_reactor.metadata
        self.annotations = {}
        self.labels = {}

        if client_version:
            logger.debug("build json was built by osbs-client %s", client_version)

        # get info about base image from dockerfile
        build_file_path, build_file_dir = self.source.get_build_file_path()

        self.df_dir = build_file_dir
        self._df_path = None
        self.original_df = None
        self.buildargs = {}  # --buildargs for container build
        self.dockerfile_images = DockerfileImages([])
        # OSBS2 TBD
        self.image_id = None
        # OSBS2 TBD
        self.parent_images_digests = {}

        # If the Dockerfile will be entirely generated from the container.yaml
        # (in the Flatpak case, say), then a plugin needs to create the Dockerfile
        # and set the base image
        if build_file_path.endswith(DOCKERFILE_FILENAME):
            self.set_df_path(build_file_path)

        # openshift in configuration needs namespace, it was reading it from get_builds_json()
        # we should have it in user_params['namespace']
        self.conf = Configuration(config_path=reactor_config_path)
        self.conf.set_workflow_based_on_config(self)

    @property
    def df_path(self):
        if self._df_path is None:
            raise AttributeError("Dockerfile has not yet been generated")

        return self._df_path

    def set_df_path(self, path):
        self._df_path = path
        dfp = df_parser(path)
        if dfp.baseimage is None:
            raise RuntimeError("no base image specified in Dockerfile")

        self.dockerfile_images = DockerfileImages(dfp.parent_images)
        logger.debug("base image specified in dockerfile = '%s'", dfp.baseimage)
        logger.debug("parent images specified in dockerfile = '%s'", dfp.parent_images)

        custom_base_images = set()
        for image in dfp.parent_images:
            image_name = ImageName.parse(image)
            image_str = image_name.to_str()
            if base_image_is_custom(image_str):
                custom_base_images.add(image_str)

        if len(custom_base_images) > 1:
            raise NotImplementedError("multiple different custom base images"
                                      " aren't allowed in Dockerfile")

        # validate user has not specified COPY --from=image
        builders = []
        for stmt in dfp.structure:
            if stmt['instruction'] == 'FROM':
                # extract "bar" from "foo as bar" and record as build stage
                match = re.search(r'\S+ \s+  as  \s+ (\S+)', stmt['value'], re.I | re.X)
                builders.append(match.group(1) if match else None)
            elif stmt['instruction'] == 'COPY':
                match = re.search(r'--from=(\S+)', stmt['value'], re.I)
                if not match:
                    continue
                stage = match.group(1)
                # error unless the --from is the index or name of a stage we've seen
                if any(stage in [str(idx), builder] for idx, builder in enumerate(builders)):
                    continue
                raise RuntimeError(dedent("""\
                    OSBS does not support COPY --from unless it matches a build stage.
                    Dockerfile instruction was:
                      {}
                    To use an image with COPY --from, specify it in a stage with FROM, e.g.
                      FROM {} AS source
                      FROM ...
                      COPY --from=source <src> <dest>
                    """).format(stmt['content'], stage))

    def parent_images_to_str(self):
        results = {}
        for base_image_name, parent_image_name in self.dockerfile_images.items():
            base_str = str(base_image_name)
            parent_str = str(parent_image_name)
            if base_image_name and parent_image_name:
                results[base_str] = parent_str
            else:
                logger.debug("None in: base %s has parent %s", base_str, parent_str)

        return results

    def get_orchestrate_build_plugin(self):
        """
        Get the orchestrate_build plugin configuration for this workflow
        if present (will be present for orchestrator, not for worker).

        :return: orchestrate_build plugin configuration dict
        :raises: ValueError if the orchestrate_build plugin is not present
        """
        for plugin in self.plugins.buildstep:
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
    def image(self):
        return self.user_params['image_tag']

    @property
    def build_process_failed(self):
        """
        Has any aspect of the build process failed?
        """
        return self.build_result.is_failed() or self.plugin_failed

    def throw_canceled_build_exception(self, *args, **kwargs):
        self.build_canceled = True
        raise BuildCanceledException("Build was canceled")

    def build_docker_image(self) -> BuildResult:
        """
        build docker image

        :return: BuildResult
        """
        print_version_of_tools()

        exception_being_handled = False
        # Make sure exit_runner is defined for finally block
        exit_runner = None
        try:
            self.fs_watcher.start()
            signal.signal(signal.SIGTERM, self.throw_canceled_build_exception)
            prebuild_runner = PreBuildPluginsRunner(self, self.plugins.prebuild,
                                                    plugin_files=self.plugin_files)
            prepublish_runner = PrePublishPluginsRunner(self, self.plugins.prepublish,
                                                        plugin_files=self.plugin_files)
            postbuild_runner = PostBuildPluginsRunner(self, self.plugins.postbuild,
                                                      plugin_files=self.plugin_files)
            # time to run pre-build plugins, so they can access cloned repo
            logger.info("running pre-build plugins")
            try:
                prebuild_runner.run()
            except PluginFailedException as ex:
                logger.error("one or more prebuild plugins failed: %s", ex)
                raise

            # we are delaying initialization, because prebuild plugin reactor_config
            # might change build method
            buildstep_runner = BuildStepPluginsRunner(self, self.plugins.buildstep,
                                                      plugin_files=self.plugin_files)

            logger.info("running buildstep plugins")
            try:
                self.build_result = buildstep_runner.run()

                if self.build_result.is_failed():
                    raise PluginFailedException(self.build_result.fail_reason)
            except PluginFailedException as ex:
                logger.error('buildstep plugin failed: %s', ex)
                raise

            if self.build_result.is_image_available():
                self.image_id = self.build_result.image_id

            # run prepublish plugins
            try:
                prepublish_runner.run()
            except PluginFailedException as ex:
                logger.error("one or more prepublish plugins failed: %s", ex)
                raise

            if self.build_result.is_image_available():
                # OSBS2 TBD
                self.built_image_inspect = imageutil.inspect_built_image()
                # OSBS2 TBD
                history = imageutil.get_image_history(self.image_id)
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

            exit_runner = ExitPluginsRunner(self, self.plugins.exit,
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
                self.source.remove_workdir()  # OSBS2 TBD: Don't remove here, remove in exit task?
                self.fs_watcher.finish()

            signal.signal(signal.SIGTERM, signal.SIG_DFL)
