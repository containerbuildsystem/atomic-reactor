"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Script for building docker image. This is expected to run inside container.
"""

import json
import logging
import tempfile

from dock.build import InsideBuilder
from dock.plugin import PostBuildPluginsRunner, PreBuildPluginsRunner, InputPluginsRunner, PrePublishPluginsRunner, \
    PluginFailedException
from dock.source import get_source_instance_for
from dock.util import ImageName


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
    def default(self, obj):
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
        self.images = []  # list of ImageName instances

    def add_image(self, image):
        """

        :param image: str, name of image (e.g. "namespace/httpd:2.4")
        :return: None
        """
        self.images.append(ImageName.parse(image))

    def add_images(self, images):
        """

        :param images: list of str, list of image names
        :return: None
        """
        for image in images:
            self.add_image(image)


class Registry(object):
    def __init__(self, uri, insecure=False):
        """
        abstraction for all registry classes

        :param uri: str, uri for pulling (in case of docker-registry, pushing too)
        :param insecure: bool
        """
        self.uri = uri
        self.insecure = insecure


class PulpRegistry(Registry):
    """ pulp & crane """
    def __init__(self, name, crane_uri, insecure=False):
        """
        :param name: str, pulp's rest api is specified in dockpulp's config, we refer only by name
        :param crane_uri: str, read-only docker registry api access point
        :param insecure: bool
        """
        super(PulpRegistry, self).__init__(crane_uri, insecure=insecure)
        self.name = name


class DockerRegistry(Registry):
    """ v1 docker registry """


class PushConf(object):
    """
    configuration of remote registries: docker-registry or pulp
    """

    def __init__(self):
        self._registries = {
            "docker": [],
            "pulp": [],
        }

    def add_docker_registry(self, registry_uri, insecure=False):
        if registry_uri is None:
            raise RuntimeError("registry URI cannot be None")
        r = DockerRegistry(registry_uri, insecure=insecure)
        self._registries["docker"].append(r)

    def add_docker_registries(self, registry_uris, insecure=False):
        for registry_uri in registry_uris:
            self.add_docker_registry(registry_uri, insecure=insecure)

    def add_pulp_registry(self, name, crane_uri):
        if crane_uri is None:
            raise RuntimeError("registry URI cannot be None")
        r = PulpRegistry(name, crane_uri)
        self._registries["pulp"].append(r)

    @property
    def has_some_docker_registry(self):
        return len(self.docker_registries) > 0

    @property
    def docker_registries(self):
        return self._registries["docker"]

    @property
    def pulp_registries(self):
        return self._registries["pulp"]

    @property
    def all_registries(self):
        return self.docker_registries + self.pulp_registries


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

    def __init__(self, source, image, parent_registry=None, target_registries=None,
                 prebuild_plugins=None, prepublish_plugins=None, postbuild_plugins=None,
                 plugin_files=None, parent_registry_insecure=False,
                 target_registries_insecure=False, dont_pull_base_image=False, **kwargs):
        """
        :param source: dict, where/how to get source code to put in image
        :param image: str, tag for built image ([registry/]image_name[:tag])
        :param parent_registry: str, registry to pull base image from
        :param target_registries: list of str, list of registries to push image to (might change in future)
        :param prebuild_plugins: dict, arguments for pre-build plugins
        :param prepublish_plugins: dict, arguments for test-build plugins
        :param postbuild_plugins: dict, arguments for post-build plugins
        :param plugin_files: list of str, load plugins also from these files
        :param parent_registry_insecure: bool, allow connecting to parent registry over plain http
        :param target_registries_insecure: bool, allow connecting to target registries over plain http
        :param dont_pull_base_image: bool, don't pull or update base image specified in dockerfile
        """
        self.source = get_source_instance_for(source, tmpdir=tempfile.mkdtemp())
        self.image = image

        self.parent_registry = parent_registry
        self.parent_registry_insecure = parent_registry_insecure

        self.prebuild_plugins_conf = prebuild_plugins
        self.prepublish_plugins_conf = prepublish_plugins
        self.postbuild_plugins_conf = postbuild_plugins
        self.prebuild_results = {}
        self.postbuild_results = {}
        self.plugin_files = plugin_files

        self.kwargs = kwargs

        self.builder = None
        self.build_logs = None
        self.built_image_inspect = None

        self.dont_pull_base_image = dont_pull_base_image
        self.pulled_base_images = set()

        # squashed image tarball
        # set by squash plugin
        self.exported_squashed_image = {}

        self.tag_conf = TagConf()
        self.tag_conf.add_image(self.image)
        self.push_conf = PushConf()
        if target_registries:
            self.push_conf.add_docker_registries(target_registries, insecure=target_registries_insecure)

        # mapping of downloaded files; DON'T PUT ANYTHING BIG HERE!
        # "path/to/file" -> "content"
        self.files = {}

        if kwargs:
            logger.warning("unprocessed keyword arguments: %s", kwargs)

    def build_docker_image(self):
        """
        build docker image

        :return: BuildResults
        """
        self.builder = InsideBuilder(self.source, self.image)
        try:
            if not self.dont_pull_base_image:
                self.pulled_base_images = self.builder.pull_base_image(self.parent_registry,
                                                                       insecure=self.parent_registry_insecure)

            # time to run pre-build plugins, so they can access cloned repo,
            # base image
            logger.info("running pre-build plugins")
            prebuild_runner = PreBuildPluginsRunner(self.builder.tasker, self, self.prebuild_plugins_conf,
                                                    plugin_files=self.plugin_files)
            try:
                prebuild_runner.run()
            except PluginFailedException as ex:
                logger.error("One or more prebuild plugins failed: %s", ex)
                return

            build_result = self.builder.build()
            self.build_logs = build_result.logs

            if not build_result.is_failed():
                self.built_image_inspect = self.builder.inspect_built_image()

            # run prepublish plugins
            prepublish_runner = PrePublishPluginsRunner(self.builder.tasker, self, self.prepublish_plugins_conf,
                                                        plugin_files=self.plugin_files)
            try:
                prepublish_runner.run()
            except PluginFailedException as ex:
                logger.error("One or more prepublish plugins failed: %s", ex)
                return

            if not build_result.is_failed():
                for registry in self.push_conf.docker_registries:
                    self.builder.push_built_image(registry.uri,
                                                  insecure=registry.insecure)

            postbuild_runner = PostBuildPluginsRunner(self.builder.tasker, self, self.postbuild_plugins_conf,
                                                      plugin_files=self.plugin_files)
            try:
                postbuild_runner.run()
            except PluginFailedException as ex:
                logger.error("One or more postbuild plugins failed: %s", ex)
                return

            return build_result
        finally:
            self.source.remove_tmpdir()

    def _prepare_response(self):
        """
        prepare response for build: gather info about images

        :return BuildResults
        """
        # FIXME: everything in here should be in separate postbuild plugin
        assert self.builder is not None
        runner = PostBuildPluginsRunner(self.builder.tasker, self, self.postbuild_plugins_conf,
                                        plugin_files=self.plugin_files)
        results = BuildResults()
        results.built_img_inspect = self.builder.inspect_built_image()
        results.built_img_info = self.builder.get_built_image_info()
        results.base_img_inspect = self.builder.inspect_base_image()
        results.base_img_info = self.builder.get_base_image_info()
        results.base_plugins_output = runner.run()  # self.builder.base_image_name
        results.built_img_plugins_output = runner.run()  # self.builder.image
        return results


def build_inside(input, input_args=None, substitutions=None):
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

    if not input:
        raise RuntimeError("No input method specified!")
    else:
        logger.debug("getting build json from input %s", input)

        cleaned_input_args = process_keyvals(input_args)
        cleaned_subs = process_keyvals(substitutions)

        cleaned_input_args['substitutions'] = cleaned_subs

        input_runner = InputPluginsRunner([{'name': input, 'args': cleaned_input_args}])
        build_json = input_runner.run()[input]
        logger.debug("Build json: %s", build_json)
    if not build_json:
        raise RuntimeError("No valid build json!")
    # TODO: validate json
    dbw = DockerBuildWorkflow(**build_json)
    build_result = dbw.build_docker_image()
    if not build_result or build_result.is_failed():
        raise RuntimeError("no image built")
    else:
        logger.info("Build has finished successfully \o/")
