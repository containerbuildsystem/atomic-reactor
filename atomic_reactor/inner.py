"""
Copyright (c) 2015-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Script for building docker image. This is expected to run inside container.
"""

import functools
import json
import logging
import signal
import threading
import os
import time
import re
from dataclasses import dataclass, field, fields
from textwrap import dedent
from typing import Any, Callable, Dict, Final, List, Optional, Union

from atomic_reactor.dirs import ContextDir, RootBuildDir
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
    PLUGIN_BUILD_ORCHESTRATE_KEY,
    REACTOR_CONFIG_FULL_PATH,
    DOCKERFILE_FILENAME,
)
from atomic_reactor.types import ISerializer
from atomic_reactor.util import (exception_message, DockerfileImages, df_parser,
                                 base_image_is_custom, print_version_of_tools, validate_with_schema)
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


class BuildResult(ISerializer):

    REMOTE_IMAGE = object()

    def __init__(self,
                 logs: Optional[List[str]] = None,
                 fail_reason: Optional[str] = None,
                 image_id: Optional[str] = None,
                 annotations: Optional[Dict[str, str]] = None,
                 labels: Optional[Dict[str, str]] = None,
                 skip_layer_squash: bool = False,
                 source_docker_archive: Optional[str] = None):
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

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, type(self)):
            return False
        return (
            self._logs == other.logs and
            self._fail_reason == other.fail_reason and
            self._image_id == other.image_id and
            self._annotations == other.annotations and
            self._labels == other.labels and
            self._skip_layer_squash == other.skip_layer_squash and
            self._source_docker_archive == other.source_docker_archive
        )

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

    def is_image_available(self) -> bool:
        return bool(self._image_id and self._image_id is not self.REMOTE_IMAGE)

    @classmethod
    def load(cls, data: Dict[str, Any]):
        return cls(
            logs=data.get("logs"),
            fail_reason=data.get("fail_reason"),
            image_id=data.get("image_id"),
            annotations=data.get("annotations"),
            labels=data.get("labels"),
            skip_layer_squash=data.get("skip_layer_squash", False),
            source_docker_archive=data.get("source_docker_archive"),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "annotations": self.annotations,
            "fail_reason": self.fail_reason,
            "image_id": self.image_id,
            "labels": self.labels,
            "logs": self.logs,
            "skip_layer_squash": self.skip_layer_squash,
            "source_docker_archive": self.source_docker_archive,
        }


class TagConf(ISerializer):
    """
    confguration of image names and tags to be applied
    """

    def __init__(self):
        # list of ImageNames with 'static' tags
        self._primary_images: List[ImageName] = []
        # list if ImageName instances with unpredictable names
        self._unique_images: List[ImageName] = []
        # list of ImageName instances with 'floating' tags
        # which can be updated by other images later
        self._floating_images: List[ImageName] = []

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, type(self)):
            return False
        return (
            self._primary_images == other.primary_images and
            self._unique_images == other.unique_images and
            self._floating_images == other.floating_images
        )

    @property
    def is_empty(self):
        return (
            len(self.primary_images) == 0 and
            len(self.unique_images) == 0 and
            len(self.floating_images) == 0
        )

    @property
    def primary_images(self):
        """
        primary image names are static and should be used for layering

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

        :return: list of ImageName
        """
        return self._unique_images

    @property
    def floating_images(self):
        """
        floating image names are floating and should be used for layering

        :return: list of ImageName
        """
        return self._floating_images

    def add_primary_image(self, image: Union[str, "ImageName"]) -> None:
        """add new primary image

        :param image: str, name of image (e.g. "namespace/httpd:2.4")
        :return: None
        """
        self._primary_images.append(ImageName.parse(image))

    def add_unique_image(self, image: Union[str, "ImageName"]) -> None:
        """add image with unpredictable name

        :param image: str, name of image (e.g. "namespace/httpd:2.4")
        :return: None
        """
        self._unique_images.append(ImageName.parse(image))

    def add_floating_image(self, image: Union[str, "ImageName"]) -> None:
        """add image with floating name

        :param image: str, name of image (e.g. "namespace/httpd:2.4")
        :return: None
        """
        self._floating_images.append(ImageName.parse(image))

    def get_unique_images_with_platform(self, platform):
        """
        Add platform to unique images

        :param platform: str, platform to be added to unique images
        return: list of unique images with added platform
        """
        return list(map(lambda unique_image: f'{unique_image}-{platform}', self._unique_images))

    @classmethod
    def load(cls, data: Dict[str, Any]):
        tag_conf = TagConf()
        image: ImageName
        for image in data.get("primary_images", []):
            tag_conf.add_primary_image(image)
        for image in data.get("unique_images", []):
            tag_conf.add_unique_image(image)
        for image in data.get("floating_images", []):
            tag_conf.add_floating_image(image)
        return tag_conf

    def as_dict(self) -> Dict[str, Any]:
        return {
            "primary_images": self.primary_images,
            "unique_images": self.unique_images,
            "floating_images": self.floating_images,
        }


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


@dataclass
class ImageBuildWorkflowData(ISerializer):
    """Manage workflow data.

    Workflow data is those data which are generated values by plugins through
    the whole build workflow (pipeline) and must be shared in every build tasks.

    These data can be dumped into dictionary object in order to be saved into a
    file as JSON data, and then be loaded while atomic-reactor launches again to
    execute another set of plugins for a different build task.
    """

    dockerfile_images: DockerfileImages = field(default_factory=DockerfileImages)
    tag_conf: TagConf = field(default_factory=TagConf)

    prebuild_results: Dict[str, Any] = field(default_factory=dict)
    buildstep_result: Dict[str, Any] = field(default_factory=dict)
    postbuild_results: Dict[str, Any] = field(default_factory=dict)
    prepub_results: Dict[str, Any] = field(default_factory=dict)
    exit_results: Dict[str, Any] = field(default_factory=dict)
    plugin_workspace: Dict[str, Any] = field(default_factory=dict)

    # Plugin name -> timestamp in isoformat
    plugins_timestamps: Dict[str, str] = field(default_factory=dict)
    # Plugin name -> seconds
    plugins_durations: Dict[str, float] = field(default_factory=dict)
    # Plugin name -> a string containing error message
    plugins_errors: Dict[str, str] = field(default_factory=dict)
    build_canceled: bool = False
    plugin_failed: bool = False

    # info about pre-declared build, build-id and token
    reserved_build_id: Optional[int] = None
    reserved_token: Optional[str] = None
    koji_source_nvr: Dict[str, str] = field(default_factory=dict)
    koji_source_source_url: Optional[str] = None
    koji_source_manifest: Dict[str, Any] = field(default_factory=dict)

    buildargs: Dict[str, str] = field(default_factory=dict)  # --buildargs for container build

    # When an image is exported into tarball, it can then be processed by various plugins.
    #  Each plugin that transforms the image should save it as a new file and append it to
    #  the end of exported_image_sequence. Other plugins should then operate with last
    #  member of this structure. Example:
    #  [{'path': '/tmp/foo.tar', 'size': 12345678, 'md5sum': '<md5>', 'sha256sum': '<sha256>'}]
    #  You can use util.get_exported_image_metadata to create a dict to append to this list.
    # OSBS2 TBD exported_image_sequence will not work for multiple platform
    exported_image_sequence: List[Dict[str, Union[str, int]]] = field(default_factory=list)

    # mapping of downloaded files; DON'T PUT ANYTHING BIG HERE!
    # "path/to/file" -> "content"
    files: Dict[str, str] = field(default_factory=dict)

    # List of RPMs that go into the final result, as per utils.rpm.parse_rpm_output
    # Each RPM inside is a mapping containing the name, version, release and other attributes.
    image_components: Optional[List[Dict[str, Union[int, str]]]] = None

    # List of all yum repos. The provided repourls might be changed (by resolve_composes) when
    # inheritance is enabled. This property holds the updated list of repos, allowing
    # post-build plugins (such as koji_import) to record them.
    all_yum_repourls: List[str] = field(default_factory=list)

    # Plugins can store info here using the @annotation, @annotation_map,
    # @label and @label_map decorators from atomic_reactor.metadata
    annotations: Dict[str, Any] = field(default_factory=dict)
    labels: Dict[str, Any] = field(default_factory=dict)

    # OSBS2 TBD
    image_id: Optional[str] = None
    parent_images_digests: Dict[str, Dict[str, str]] = field(default_factory=dict)

    build_result: BuildResult = field(init=False)

    def __post_init__(self):
        # TBD: value will be determined when implement the build task.
        self.build_result = BuildResult(fail_reason="not built")

    @classmethod
    def load(cls, data: Dict[str, Any]):
        """Load workflow data from given input."""
        wf_data = cls()

        if not data:
            return wf_data

        data_conv: Dict[str, Callable] = {
            "dockerfile_images": DockerfileImages.load,
            "tag_conf": TagConf.load,
            "build_result": BuildResult.load,
        }

        def _return_directly(value):
            return value

        defined_field_names = set(f.name for f in fields(cls))
        for name, value in data.items():
            if name not in defined_field_names:
                logger.info("Unknown field name %s", name)
                continue
            setattr(wf_data, name, data_conv.get(name, _return_directly)(value))
        return wf_data

    @classmethod
    def load_from_dir(cls, context_dir: ContextDir) -> "ImageBuildWorkflowData":
        """Load workflow data from the data directory.

        :param context_dir: a directory holding the files containing the serialized
            workflow data.
        :type context_dir: ContextDir
        :return: the workflow data containing data loaded from the specified directory.
        :rtype: ImageBuildWorkflowData
        """

        # TBD: build result should be handled specifically after the build
        #      task is done. There will be multiple build result objects for
        #      multi-platforms and they should be written in to separate
        #      build_result.json file in each platform directory.

        if not context_dir.workflow_json.exists():
            return cls()

        with open(context_dir.workflow_json, "r") as f:
            file_content = f.read()

        raw_data = json.loads(file_content)
        validate_with_schema(raw_data, "schemas/workflow_data.json")

        # NOTE: json.loads twice since the data is validated at the first time.
        workflow_data = json.loads(file_content, object_hook=WorkflowDataDecoder())

        build_result = workflow_data.pop("build_result")
        loaded_data = cls(**workflow_data)
        loaded_data.build_result = build_result
        return loaded_data

    def as_dict(self) -> Dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    def save(self, context_dir: ContextDir) -> None:
        """Save workflow data into the files under a specific directory.

        :param context_dir: a directory holding the files containing the serialized
            workflow data.
        :type context_dir: ContextDir
        """

        # TBD: same comment as above for build_result

        logger.info("Writing workflow data into %s", context_dir.workflow_json)
        with open(context_dir.workflow_json, "w+") as f:
            json.dump(self.as_dict(), f, cls=WorkflowDataEncoder)


class WorkflowDataEncoder(json.JSONEncoder):
    """Convert custom serializable objects into dict as JSON data."""

    def default(self, o: object) -> Any:
        if isinstance(o, ISerializer):
            data = o.as_dict()
            # Data type name used to know which type of object to recover.
            data["__type__"] = o.__class__.__name__
            return data
        elif isinstance(o, ImageName):
            return {
                "__type__": o.__class__.__name__,
                "str": o.to_str(),
            }
        return super().default(o)


class WorkflowDataDecoder:
    """Custom JSON decoder for workflow data."""

    def _restore_image_name(self, data: Dict[str, str]) -> ImageName:
        """Factor to create an ImageName object."""
        return ImageName.parse(data["str"])

    def __call__(self, data: Dict[str, Any]) -> Any:
        """Restore custom serializable objects."""
        loader_meths: Final[Dict[str, Callable]] = {
            BuildResult.__name__: BuildResult.load,
            DockerfileImages.__name__: DockerfileImages.load,
            TagConf.__name__: TagConf.load,
            ImageName.__name__: self._restore_image_name,
        }
        if "__type__" not in data:
            # __type__ is an identifier to indicate a dict object represents an
            # object that should be recovered. If no type is included, just
            # treat it as a normal dict and return.
            return data
        obj_type = data.pop("__type__")
        loader_meth = loader_meths.get(obj_type)
        if loader_meth is None:
            raise ValueError(
                f"Unknown object type {obj_type} to restore an object from data {data!r}."
            )
        return loader_meth(data)


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
    _default_user_params: Dict[str, Any] = {}

    def __init__(
        self,
        build_dir: RootBuildDir,
        data: Optional[ImageBuildWorkflowData] = None,
        source: Source = None,
        plugins: PluginsDef = None,
        user_params: dict = None,
        reactor_config_path: str = REACTOR_CONFIG_FULL_PATH,
        plugin_files: List[str] = None,
        client_version: str = None,
    ):
        """
        :param build_dir: a directory holding all the artifacts to build an image.
        :type build_dir: RootBuildDir
        :param data:
        :type data: ImageBuildWorkflowData
        :param source: where/how to get source code to put in image
        :param plugins: the plugins to be executed in this workflow
        :param user_params: user (and other) params that control various aspects of the build
        :param reactor_config_path: path to atomic-reactor configuration file
        :param plugin_files: load plugins also from these files
        :param client_version: osbs-client version used to render build json
        """
        self.build_dir = build_dir
        self.data = data or ImageBuildWorkflowData()

        self.source = source or DummySource(None, None)
        self.plugins = plugins or PluginsDef()
        self.user_params = user_params or self._default_user_params.copy()

        self.plugin_files = plugin_files
        self.fs_watcher = FSWatcher()

        self.storage_transport = DOCKER_STORAGE_TRANSPORT_NAME

        if client_version:
            logger.debug("build json was built by osbs-client %s", client_version)

        # get info about base image from dockerfile
        build_file_path, build_file_dir = self.source.get_build_file_path()

        self.df_dir = build_file_dir
        self._df_path: Optional[str] = None

        # openshift in configuration needs namespace, it was reading it from get_builds_json()
        # we should have it in user_params['namespace']
        self.conf = Configuration(config_path=reactor_config_path)

        # If the Dockerfile will be entirely generated from the container.yaml
        # (in the Flatpak case, say), then a plugin needs to create the Dockerfile
        # and set the base image
        if build_file_path.endswith(DOCKERFILE_FILENAME):
            self.reset_dockerfile_images(build_file_path)

    @property
    def df_path(self):
        if self._df_path is None:
            raise AttributeError("Dockerfile has not yet been generated")

        return self._df_path

    def reset_dockerfile_images(self, path: str) -> None:
        """Given a new Dockerfile path, (re)set all the mutable state that relates to it.

        Workflow keeps a dockerfile_images object, which corresponds to the parent images in
        the Dockerfile. This object and the actual Dockerfile are both mutable (and plugins
        frequently mutate them). It is the responsibility of every plugin to make the changes
        in such a way that the actual parent images and their in-memory representation do not
        get out of sync.

        For extreme cases such as plugins creating an entirely new Dockerfile (e.g.
        flatpak_create_dockerfile), this method *must* be used to replace the existing
        dockerfile_images object with a new one and re-apply some mutations.
        """
        self._df_path = path  # OSBS2 TBD: delete this

        df_images = self.data.dockerfile_images

        # Consider dockerfile_images data was saved when previous task ended,
        # e.g. prebuild, now subsequent task starts to run and the saved data
        # is loaded into the dockerfile_images object. In this case, no need
        # to update the restored dockerfile_images data.
        if df_images.is_empty:
            df_images = self._parse_dockerfile_images(path)
            self.data.dockerfile_images = df_images
            self.conf.update_dockerfile_images_from_config(df_images)

        # But, still need to do this
        self.imageutil.set_dockerfile_images(df_images)

    def _parse_dockerfile_images(self, path: str) -> DockerfileImages:
        dfp = df_parser(path)
        if dfp.baseimage is None:
            raise RuntimeError("no base image specified in Dockerfile")

        dockerfile_images = DockerfileImages(dfp.parent_images)
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

        return dockerfile_images

    @functools.cached_property
    def imageutil(self) -> imageutil.ImageUtil:
        """Get an ImageUtil instance.

        The property is lazy, subsequent calls will return the same instance. This is important
        for performance reasons (ImageUtil caches registry queries, a new instance would not have
        the cache).
        """
        return imageutil.ImageUtil(self.data.dockerfile_images, self.conf)

    def parent_images_to_str(self):
        results = {}
        for base_image_name, parent_image_name in self.data.dockerfile_images.items():
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
        return self.data.build_result.is_failed() or self.data.plugin_failed

    def throw_canceled_build_exception(self, *args, **kwargs):
        self.data.build_canceled = True
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
                self.data.build_result = buildstep_runner.run()

                if self.data.build_result.is_failed():
                    raise PluginFailedException(self.data.build_result.fail_reason)
            except PluginFailedException as ex:
                logger.error('buildstep plugin failed: %s', ex)
                raise

            if self.data.build_result.is_image_available():
                self.data.image_id = self.data.build_result.image_id

            # run prepublish plugins
            try:
                prepublish_runner.run()
            except PluginFailedException as ex:
                logger.error("one or more prepublish plugins failed: %s", ex)
                raise

            try:
                postbuild_runner.run()
            except PluginFailedException as ex:
                logger.error("one or more postbuild plugins failed: %s", ex)
                raise

            return self.data.build_result
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
                self.fs_watcher.finish()

            signal.signal(signal.SIGTERM, signal.SIG_DFL)
