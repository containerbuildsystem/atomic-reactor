"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import, unicode_literals

import logging
import os.path

from osbs.utils import Labels

from atomic_reactor import util
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.constants import PLUGIN_PIN_OPERATOR_DIGESTS_KEY, INSPECT_CONFIG
from atomic_reactor.util import (has_operator_bundle_manifest,
                                 get_manifest_digests,
                                 read_yaml_from_file_path,
                                 ImageName)
from atomic_reactor.plugins.pre_reactor_config import get_operator_manifests
from atomic_reactor.plugins.build_orchestrate_build import override_build_kwarg
from atomic_reactor.utils.operator import OperatorManifest


class PinOperatorDigestsPlugin(PreBuildPlugin):
    """
    Plugin runs for operator manifest bundle builds.

    When running in orchestrator:
    - finds container pullspecs in operator ClusterServiceVersion files
    - computes replacement pullspecs:
        - replaces tags with manifest list digests
        - replaces repos (and namespaces) based on operator_manifests.repo_replacements
          configuration in container.yaml and r-c-m*
        - replaces registries based on operator_manifests.registry_post_replace in r-c-m*

    When running in a worker:
    - receives replacement pullspec mapping computed by orchestrator
    - replaces pullspecs in ClusterServiceVersion files based on said mapping

    * reactor-config-map
    """

    key = PLUGIN_PIN_OPERATOR_DIGESTS_KEY
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, replacement_pullspecs=None):
        """
        Initialize pin_operator_digests plugin

        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param replacement_pullspecs: Dict[str, str], computed in orchestrator,
                                      provided to workers by osbs-client
        """
        super(PinOperatorDigestsPlugin, self).__init__(tasker, workflow)
        self.user_config = workflow.source.config.operator_manifests
        self.site_config = None  # Only relevant (and available) in orchestrator
        self.replacement_pullspecs = replacement_pullspecs

    def run(self):
        """
        Run pin_operator_digest plugin
        """
        if self.should_run():
            if self.is_in_orchestrator():
                return self.run_in_orchestrator()
            else:
                return self.run_in_worker()

    def should_run(self):
        """
        Determine if this is an operator manifest bundle build

        :return: bool, should plugin run?
        """
        if has_operator_bundle_manifest(self.workflow):
            return True
        else:
            self.log.info("Not an operator manifest bundle build, skipping plugin")
            return False

    def run_in_orchestrator(self):
        """
        Run plugin in orchestrator. Find all image pullspecs,
        compute their replacements and set build arg for worker
        """
        try:
            self.site_config = get_operator_manifests(self.workflow)
        except KeyError:
            msg = "operator_manifests configuration missing in reactor config map, aborting"
            self.log.warning(msg)
            return

        operator_manifest = self._get_operator_manifest()
        pullspecs = self._get_pullspecs(operator_manifest)

        if pullspecs:
            replacement_pullspecs = self._get_replacement_pullspecs(pullspecs)
            self._set_worker_arg(replacement_pullspecs)

    def run_in_worker(self):
        """
        Run plugin in worker. Replace image pullspecs
        based on replacements computed in orchestrator.
        """
        if not self.replacement_pullspecs:
            self.log.info("No pullspecs need to be replaced")
            return

        operator_manifest = self._get_operator_manifest()
        replacement_pullspecs = {
            ImageName.parse(old): ImageName.parse(new)
            for old, new in self.replacement_pullspecs.items()
        }

        for operator_csv in operator_manifest.files:
            self.log.info("Replacing pullspecs in %s", operator_csv.path)
            operator_csv.replace_pullspecs(replacement_pullspecs)
            operator_csv.dump()

    def _get_operator_manifest(self):
        if self.user_config is None:
            raise RuntimeError("operator_manifests configuration missing in container.yaml")

        manifests_dir = os.path.join(self.workflow.source.path, self.user_config["manifests_dir"])
        self.log.info("Looking for operator CSV files in %s", manifests_dir)

        operator_manifest = OperatorManifest.from_directory(manifests_dir)

        if operator_manifest.files:
            path_lines = "\n".join(f.path for f in operator_manifest.files)
            self.log.info("Found operator CSV files:\n%s", path_lines)
        else:
            self.log.info("No operator CSV files found")

        return operator_manifest

    def _get_pullspecs(self, operator_manifest):
        pullspecs = set()
        for operator_csv in operator_manifest.files:
            pullspecs.update(operator_csv.get_pullspecs())

        if pullspecs:
            pullspec_lines = "\n".join(sorted(map(str, pullspecs)))
            self.log.info("Found pullspecs:\n%s", pullspec_lines)
        else:
            self.log.info("No pullspecs found")

        return pullspecs

    def _get_replacement_pullspecs(self, pullspecs):
        self.log.info("Computing replacement pullspecs")
        replacer = PullspecReplacer(user_config=self.user_config, site_config=self.site_config)

        for p in pullspecs:
            if not replacer.registry_is_allowed(p):
                raise RuntimeError("Registry not allowed: {} (in {})".format(p.registry, p))

        replacements = {}

        for original in pullspecs:
            self.log.info("Computing replacement for %s", original)

            self.log.debug("Making sure tag is manifest list digest")
            pinned = replacer.pin_digest(original)

            self.log.debug("Replacing namespace/repo")
            repo_replaced = replacer.replace_repo(pinned)

            self.log.debug("Replacing registry")
            registry_replaced = replacer.replace_registry(repo_replaced)

            self.log.info("Final pullspec: %s", registry_replaced)

            if registry_replaced != original:
                replacements[original] = registry_replaced

        replacement_lines = "\n".join(
            "{} -> {}".format(p, replacements[p]) if p in replacements
            else "{} - no change".format(p)
            for p in sorted(pullspecs, key=str)  # ImageName does not implement ordering
        )
        self.log.info("To be replaced:\n%s", replacement_lines)

        return replacements

    def _set_worker_arg(self, replacement_pullspecs):
        arg = {str(old): str(new) for old, new in replacement_pullspecs.items()}
        override_build_kwarg(self.workflow, "operator_bundle_replacement_pullspecs", arg)


_KEEP = object()


class PullspecReplacer(object):
    """
    Helper that takes care of replacing parts of image pullspecs
    """

    def __init__(self, user_config, site_config):
        """
        Initialize a PullspecReplacer

        :param user_config: container.yaml operator_manifest configuration
        :param site_config: reactor-config-map operator_manifests configuration
        """
        log_name = "atomic_reactor.plugins.{}".format(PinOperatorDigestsPlugin.key)
        self.log = logging.getLogger(log_name)

        self.allowed_registries = site_config["allowed_registries"]
        if self.allowed_registries is not None:
            self.allowed_registries = set(self.allowed_registries)

        self.registry_replace = {
            registry["old"]: registry["new"]
            for registry in site_config.get("registry_post_replace", [])
        }

        self.package_mapping_files = {
            mapping["registry"]: mapping["package_mappings_file"]
            for mapping in site_config.get("repo_replacements", [])
        }
        # Mapping of [file path => package mapping]
        # Loaded when needed, see _get_site_mapping
        self.file_package_mappings = {}

        self.user_package_mappings = {
            mapping["registry"]: mapping["package_mappings"]
            for mapping in user_config.get("repo_replacements", [])
        }
        # Final package mapping that you get by combining site mapping with user mapping
        # Loaded when needed, see _get_final_mapping
        self.final_package_mappings = {}

    def registry_is_allowed(self, image):
        """
        Is image registry allowed in OSBS config?

        :param image: ImageName
        :return: bool
        """
        return self.allowed_registries is None or image.registry in self.allowed_registries

    def pin_digest(self, image):
        """
        Replace image tag with manifest list digest

        :param image: ImageName
        :return: ImageName
        """
        if image.tag.startswith("sha256:"):
            self.log.debug("%s looks like a digest, skipping query", image.tag)
            return image
        self.log.debug("Querying %s for manifest list digest", image.registry)
        digests = get_manifest_digests(image, image.registry, versions=("v2_list",))
        return self._replace(image, tag=digests["v2_list"])

    def replace_registry(self, image):
        """
        Replace image registry based on OSBS config

        :param image: ImageName
        :return: ImageName
        """
        if image.registry not in self.registry_replace:
            self.log.debug("registry_post_replace not configured for %s", image.registry)
            return image
        return self._replace(image, registry=self.registry_replace[image.registry])

    def replace_repo(self, image):
        """
        Replace image repo based on OSBS site/user configuration for image registry

        Note: repo can also mean "namespace/repo"

        :param image: ImageName
        :return: ImageName
        """
        site_mapping = self._get_site_mapping(image.registry)
        if site_mapping is None and image.registry not in self.user_package_mappings:
            self.log.debug("repo_replacements not configured for %s", image.registry)
            return image

        package = self._get_component_name(image)
        mapping = self._get_final_mapping(image.registry, package)
        replacements = mapping.get(package)

        if replacements is None:
            raise RuntimeError("Replacement not configured for package {} (from {})"
                               .format(package, image))
        elif len(replacements) > 1:
            options = ", ".join(replacements)
            raise RuntimeError("Multiple replacements for package {} (from {}): {}"
                               .format(package, image, options))

        self.log.debug("Replacement for package %s: %s", package, replacements[0])
        replacement = ImageName.parse(replacements[0])
        return self._replace(image, namespace=replacement.namespace, repo=replacement.repo)

    def _get_site_mapping(self, registry):
        """
        Get the package mapping file for the given registry. If said file has
        not yet been read, read it and save mapping for later. Return mapping.
        """
        mapping_file = self.package_mapping_files.get(registry)

        if mapping_file is None:
            return None
        elif mapping_file in self.file_package_mappings:
            return self.file_package_mappings[mapping_file]

        mapping = read_yaml_from_file_path(mapping_file, "schemas/package_mapping.json")
        self.file_package_mappings[mapping_file] = mapping
        return mapping

    def _get_component_name(self, image):
        """
        Get package for image by querying registry and looking at labels.
        """
        self.log.debug("Querying %s for image labels", image.registry)
        # Do not import get_inspect_for_image directly, needs to be mocked in tests
        inspect = util.get_inspect_for_image(image, image.registry)
        labels = Labels(inspect[INSPECT_CONFIG].get("Labels", {}))

        try:
            _, package = labels.get_name_and_value(Labels.LABEL_TYPE_COMPONENT)
            self.log.debug("Resolved package name: %s", package)
        except KeyError:
            raise RuntimeError("Image has no component label: {}".format(image))

        return package

    def _get_final_mapping(self, registry, package):
        """
        Get final mapping for given registry (combine site and user mappings).

        Build final mapping package by package. If the user configures the
        replacement for a package incorrectly, build should only fail when
        replacing repos for that specific package, not before.
        """
        mapping = self.final_package_mappings.setdefault(registry, {})
        if package in mapping:
            return mapping

        site_mapping = self._get_site_mapping(registry) or {}
        user_mapping = self.user_package_mappings.get(registry, {})

        if package in user_mapping:
            replacement = user_mapping[package]
            if package not in site_mapping or replacement in site_mapping[package]:
                self.log.debug("User set replacement for package %s: %s", package, replacement)
                # Mapping file is [package => list of repos], user mapping is [package => repo]
                # Stick to [package => list of repos]
                mapping[package] = [replacement]
            else:
                choices = ", ".join(site_mapping[package])
                raise RuntimeError("Invalid replacement for package {}: {} (choices: {})"
                                   .format(package, replacement, choices))
        elif package in site_mapping:
            mapping[package] = site_mapping[package]

        return mapping

    def _replace(self, image, registry=_KEEP, namespace=_KEEP, repo=_KEEP, tag=_KEEP):
        """
        Replace specified parts of image pullspec, keep the rest
        """
        return ImageName(
            registry=image.registry if registry is _KEEP else registry,
            namespace=image.namespace if namespace is _KEEP else namespace,
            repo=image.repo if repo is _KEEP else repo,
            tag=image.tag if tag is _KEEP else tag,
        )
