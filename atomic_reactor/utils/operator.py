"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import, unicode_literals

import os
import logging

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from atomic_reactor.util import ImageName, chain_get


yaml = YAML()
log = logging.getLogger(__name__)


OPERATOR_CSV_KIND = "ClusterServiceVersion"


class NotOperatorCSV(Exception):
    """
    Data is not from a valid ClusterServiceVersion document
    """


class OperatorCSV(object):
    """
    A single ClusterServiceVersion file in an operator manifest.

    Can find and replace pullspecs for container images in predefined locations.
    """

    def __init__(self, path, data):
        """
        Initialize an OperatorCSV

        :param path: Path where data was found or where it should be written
        :param data: ClusterServiceVersion yaml data
        """
        if data.get("kind") != OPERATOR_CSV_KIND:
            raise NotOperatorCSV("Not a ClusterServiceVersion")
        self.path = path
        self.data = data

    @classmethod
    def from_file(cls, path):
        """
        Make an OperatorCSV from a file

        :param path: Path to file
        :return: OperatorCSV
        """
        with open(path) as f:
            data = yaml.load(f)
            return cls(path, data)

    def dump(self):
        """
        Write data to file (preserves comments)
        """
        with open(self.path, "w") as f:
            yaml.dump(self.data, f)

    def get_pullspecs(self):
        """
        Find pullspecs in predefined locations.

        :return: set of ImageName pullspecs
        """
        pullspecs = self._get_related_image_pullspecs()
        pullspecs.update(self._get_env_pullspecs())
        pullspecs.update(self._get_container_image_pullspecs())
        pullspecs.update(self._get_annotations_pullspecs())
        pullspecs.update(self._get_init_container_pullspecs())
        return pullspecs

    def replace_pullspecs(self, replacement_pullspecs):
        """
        Replace pullspecs in predefined locations.

        :param replacement_pullspecs: mapping of pullspec -> replacement
        """
        self._replace_related_image_pullspecs(replacement_pullspecs)
        self._replace_env_pullspecs(replacement_pullspecs)
        self._replace_container_image_pullspecs(replacement_pullspecs)
        self._replace_annotation_pullspecs(replacement_pullspecs)
        self._replace_init_container_pullspecs(replacement_pullspecs)

    def replace_pullspecs_everywhere(self, replacement_pullspecs):
        """
        Replace all pullspecs found anywhere in data

        :param replacement_pullspecs: mapping of pullspec -> replacment
        """
        log_template = "%(path)s - Replaced pullspec: %(old)s -> %(new)s"
        for k in self.data:
            self._replace_pullspecs_everywhere(self.data, k, replacement_pullspecs, log_template)

    def _get_related_images(self):
        related_images_path = ("spec", "relatedImages")
        return chain_get(self.data, related_images_path, default=[])

    def _get_deployments(self):
        deployments_path = ("spec", "install", "spec", "deployments")
        return chain_get(self.data, deployments_path, default=[])

    def _get_containers(self):
        deployments = self._get_deployments()
        containers_path = ("spec", "template", "spec", "containers")
        containers = [
            c for d in deployments for c in chain_get(d, containers_path, default=[])
        ]
        return containers

    def _get_annotations(self):
        annotations_path = ("metadata", "annotations")
        return chain_get(self.data, annotations_path, default={})

    def _get_related_image_envs(self):
        containers = self._get_containers() + self._get_init_containers()
        envs = [
            e for c in containers
            for e in c.get("env", []) if e["name"].startswith("RELATED_IMAGE_")
        ]
        for env in envs:
            if "valueFrom" in env:
                msg = '{}: "valueFrom" references are not supported'.format(env["name"])
                raise RuntimeError(msg)
        return envs

    def _get_init_containers(self):
        deployments = self._get_deployments()
        init_containers_path = ("spec", "template", "spec", "initContainers")
        init_containers = [
            c for d in deployments for c in chain_get(d, init_containers_path, default=[])
        ]
        return init_containers

    def _get_pullspec(self, obj, key, log_template=None):
        pullspec = ImageName.parse(obj[key])
        if log_template is not None:
            name = obj.get("name") if isinstance(obj, CommentedMap) else None
            log.debug(log_template,
                      {"path": self.path, "name": name, "pullspec": pullspec})
        return pullspec

    def _get_related_image_pullspecs(self):
        rel_images = self._get_related_images()
        log_template = "%(path)s - Found pullspec for related image %(name)s: %(pullspec)s"
        return set(self._get_pullspec(i, "image", log_template) for i in rel_images)

    def _get_env_pullspecs(self):
        envs = self._get_related_image_envs()
        log_template = "%(path)s - Found pullspec in %(name)s var: %(pullspec)s"
        return set(self._get_pullspec(e, "value", log_template) for e in envs)

    def _get_container_image_pullspecs(self):
        containers = self._get_containers()
        log_template = "%(path)s - Found pullspec for container %(name)s: %(pullspec)s"
        return set(self._get_pullspec(c, "image", log_template) for c in containers)

    def _get_annotations_pullspecs(self):
        annotations = self._get_annotations()
        log_template = "%(path)s - Found pullspec in annotations: %(pullspec)s"
        pullspecs = set()
        if "containerImage" in annotations:
            pullspecs.add(self._get_pullspec(annotations, "containerImage", log_template))
        return pullspecs

    def _get_init_container_pullspecs(self):
        init_containers = self._get_init_containers()
        log_template = "%(path)s - Found pullspec for initContainer %(name)s: %(pullspec)s"
        return set(self._get_pullspec(c, "image", log_template) for c in init_containers)

    def _replace_pullspec(self, obj, key, replacement_pullspecs, log_template=None):
        old = ImageName.parse(obj[key])
        new = replacement_pullspecs.get(old)
        if new is None or new == old:
            return
        if log_template is not None:
            name = obj.get("name") if isinstance(obj, CommentedMap) else None
            log.debug(log_template,
                      {"path": self.path, "name": name, "old": old, "new": new})
        obj[key] = str(new)  # `new` is an ImageName

    def _replace_related_image_pullspecs(self, replacement_pullspecs):
        related_images = self._get_related_images()
        log_tmpl = "%(path)s - Replaced pullspec for related image %(name)s: %(old)s -> %(new)s"
        for i in related_images:
            self._replace_pullspec(i, "image", replacement_pullspecs, log_tmpl)

    def _replace_env_pullspecs(self, replacement_pullspecs):
        envs = self._get_related_image_envs()
        log_template = "%(path)s - Replaced pullspec in %(name)s var: %(old)s -> %(new)s"
        for e in envs:
            self._replace_pullspec(e, "value", replacement_pullspecs, log_template)

    def _replace_container_image_pullspecs(self, replacement_pullspecs):
        containers = self._get_containers()
        log_template = "%(path)s - Replaced pullspec for container %(name)s: %(old)s -> %(new)s"
        for c in containers:
            self._replace_pullspec(c, "image", replacement_pullspecs, log_template)

    def _replace_annotation_pullspecs(self, replacement_pullspecs):
        annotations = self._get_annotations()
        log_tmpl = "%(path)s - Replaced pullspec in annotations: %(old)s -> %(new)s"
        if "containerImage" in annotations:
            self._replace_pullspec(annotations, "containerImage", replacement_pullspecs, log_tmpl)

    def _replace_init_container_pullspecs(self, replacement_pullspecs):
        init_containers = self._get_init_containers()
        log_template = (
            "%(path)s - Replaced pullspec for initContainer %(name)s: %(old)s -> %(new)s"
        )
        for c in init_containers:
            self._replace_pullspec(c, "image", replacement_pullspecs, log_template)

    def _replace_pullspecs_everywhere(self, obj, k_or_i, replacement_pullspecs, log_template):
        item = obj[k_or_i]
        if isinstance(item, CommentedMap):
            for k in item:
                self._replace_pullspecs_everywhere(item, k, replacement_pullspecs, log_template)
        elif isinstance(item, CommentedSeq):
            for i in range(len(item)):
                self._replace_pullspecs_everywhere(item, i, replacement_pullspecs, log_template)
        elif isinstance(item, str):
            # Doesn't matter if string was not a pullspec, it will simply not match anything
            # in replacement_pullspecs and no replacement will be done
            self._replace_pullspec(obj, k_or_i, replacement_pullspecs, log_template)


class OperatorManifest(object):
    """
    A collection of operator files.

    Currently, only ClusterServiceVersion files are considered relevant.
    """

    def __init__(self, files):
        """
        Initialize an OperatorManifest

        :param files: list of OperatorCSVs
        """
        self.files = files

    @classmethod
    def from_directory(cls, path):
        """
        Make an OperatorManifest from all the relevant files found in
        a directory (or its subdirectories)

        :param path: Path to directory
        :return: OperatorManifest
        """
        if not os.path.isdir(path):
            raise RuntimeError("Path does not exist or is not a directory: {}".format(path))
        yaml_files = cls._get_yaml_files(path)
        operator_csvs = list(cls._get_csvs(yaml_files))
        return cls(operator_csvs)

    @classmethod
    def _get_yaml_files(cls, dirpath):
        for d, _, files in os.walk(dirpath):
            for f in files:
                if f.endswith(".yaml") or f.endswith(".yml"):
                    yield os.path.join(d, f)

    @classmethod
    def _get_csvs(cls, yaml_files):
        for f in yaml_files:
            try:
                yield OperatorCSV.from_file(f)
            except NotOperatorCSV:
                pass
