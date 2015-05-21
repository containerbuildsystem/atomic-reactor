"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Script for building docker image. This is expected to run inside container.
"""

import json
import logging
import shutil
import tempfile

from dock.build import InsideBuilder
from dock.plugin import PostBuildPluginsRunner, PreBuildPluginsRunner, InputPluginsRunner, PrePublishPluginsRunner, \
    PluginFailedException


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


class TagAndPushConf(object):
    """
    mapping =
      {
        "<registry_uri>": {
          "insecure": false,
          "image_names": [
            "image-name1",
            "prefix/image-name2",
          ],
        }
        "...": {...}
      }
    """

    def __init__(self, mapping=None):
        self.mapping = mapping or {}

    @property
    def registries(self):
        return self.mapping.keys()

    def __getitem__(self, item):
        return self.mapping[item]

    def init_registry_conf(self, registry, insecure=False):
        """ initialize new registry in mapping (do nothing if it's already there) """
        self.mapping.setdefault(registry, {})
        self.mapping[registry].setdefault("insecure", insecure)
        self.mapping[registry].setdefault("image_names", [])

    def add_image(self, registry, image, insecure=False):
        self.init_registry_conf(registry, insecure)
        self.mapping[registry]['image_names'].append(image)

    def add_images(self, registry, images, insecure=False):
        self.init_registry_conf(registry, insecure)
        self.mapping[registry]['image_names'] += images

    def merge_with_mapping(self, mapping):
        if not isinstance(mapping, dict):
            return
        for registry_uri, registry_conf in mapping.items():
            insecure = registry_conf.get("insecure", None)
            if insecure is None:
                self.init_registry_conf(registry_uri)
            else:
                self.init_registry_conf(registry_uri, insecure)
            self.add_images(registry_uri, registry_conf.get("image_names", []))


class DockerBuildWorkflow(object):
    """
    This class defines a workflow for building images:

    1. pull image from registry
    2. tag it properly if needed
    3. clone git repo
    4. build image
    5. tag it
    6. push it to registries
    """

    def __init__(self, git_url, image, git_dockerfile_path=None,
                 git_commit=None, parent_registry=None, target_registries=None,
                 prebuild_plugins=None, prepublish_plugins=None, postbuild_plugins=None,
                 plugin_files=None, parent_registry_insecure=False,
                 target_registries_insecure=False, **kwargs):
        """
        :param git_url: str, URL to git repo
        :param image: str, tag for built image ([registry/]image_name[:tag])
        :param git_dockerfile_path: str, path to dockerfile within git repo (if not in root)
        :param git_commit: str, git commit to check out
        :param parent_registry: str, registry to pull base image from
        :param target_registries: list of str, list of registries to push image to (might change in future)
        :param prebuild_plugins: dict, arguments for pre-build plugins
        :param prepublish_plugins: dict, arguments for test-build plugins
        :param postbuild_plugins: dict, arguments for post-build plugins
        :param plugin_files: list of str, load plugins also from these files
        :param parent_registry_insecure: bool, allow connecting to parent registry over plain http
        :param target_registries_insecure: bool, allow connecting to target registries over plain http
        """
        self.git_url = git_url
        self.image = image
        self.git_dockerfile_path = git_dockerfile_path
        self.git_commit = git_commit

        self.parent_registry = parent_registry
        self.parent_registry_insecure = parent_registry_insecure
        self.target_registries = target_registries
        self.target_registries_insecure = target_registries_insecure

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

        self.pulled_base_image = None

        # TODO: ensure this is the only way to tag and push images,
        #       get rid of target_reg*, push_built_img
        self.tag_and_push_conf = TagAndPushConf()

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
        tmpdir = tempfile.mkdtemp()
        self.builder = InsideBuilder(self.git_url, self.image, git_dockerfile_path=self.git_dockerfile_path,
                                     git_commit=self.git_commit, tmpdir=tmpdir)
        try:
            self.pulled_base_image = self.builder.pull_base_image(
                self.parent_registry, insecure=self.parent_registry_insecure)

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
                if self.target_registries:
                    for target_registry in self.target_registries:
                        self.builder.push_built_image(target_registry, insecure=self.target_registries_insecure)

            postbuild_runner = PostBuildPluginsRunner(self.builder.tasker, self, self.postbuild_plugins_conf,
                                                      plugin_files=self.plugin_files)
            try:
                postbuild_runner.run()
            except PluginFailedException as ex:
                logger.error("One or more postbuild plugins failed: %s", ex)
                return

            return build_result
        finally:
            shutil.rmtree(tmpdir)

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
