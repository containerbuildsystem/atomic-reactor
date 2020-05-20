"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import, unicode_literals

import logging
import os.path

from osbs.utils import Labels, ImageName

from atomic_reactor import util
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.constants import (
    PLUGIN_PIN_OPERATOR_DIGESTS_KEY,
    INSPECT_CONFIG,
    REPO_CONTAINER_CONFIG,
)
from atomic_reactor.util import (has_operator_bundle_manifest,
                                 get_manifest_digests,
                                 read_yaml_from_url)
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
    - creates relatedImages sections in ClusterServiceVersion files

    Files that already have a relatedImages section are excluded.

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
        self.site_config = None
        self.replacement_pullspecs = replacement_pullspecs or {}

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
        if not has_operator_bundle_manifest(self.workflow):
            self.log.info("Not an operator manifest bundle build, skipping plugin")
            return False
        if get_operator_manifests(self.workflow, fallback=None) is None:
            msg = "operator_manifests configuration missing in reactor config map, aborting"
            self.log.warning(msg)
            return False
        return True

    def run_in_orchestrator(self):
        """
        Run plugin in orchestrator. Find all image pullspecs,
        compute their replacements and set build arg for worker.

        Exclude CSVs which already have a relatedImages section.
        """
        self.site_config = get_operator_manifests(self.workflow)

        operator_manifest = self._get_operator_manifest()
        pullspecs = self._get_pullspecs(operator_manifest)

        if pullspecs:
            replacement_pullspecs = self._get_replacement_pullspecs(pullspecs)
            self._set_worker_arg(replacement_pullspecs)

    def run_in_worker(self):
        """
        Run plugin in worker. Replace image pullspecs based on replacements
        computed in orchestrator, then create relatedImages sections in CSVs.

        Exclude CSVs which already have a relatedImages section.
        """
        operator_manifest = self._get_operator_manifest()
        replacement_pullspecs = {
            ImageName.parse(old): ImageName.parse(new)
            for old, new in self.replacement_pullspecs.items()
        }

        self.log.info("Updating operator CSV files")

        for operator_csv in operator_manifest.files:
            if not operator_csv.has_related_images():
                self.log.info("Replacing pullspecs in %s", operator_csv.path)
                # Replace pullspecs everywhere, not just in locations in which they
                # are expected to be found - OCP 4.4 workaround
                operator_csv.replace_pullspecs_everywhere(replacement_pullspecs)

                self.log.info("Creating relatedImages section in %s", operator_csv.path)
                operator_csv.set_related_images()

                operator_csv.dump()
            else:
                self.log.warning("%s has a relatedImages section, skipping", operator_csv.path)

    def _get_operator_manifest(self):
        if self.user_config is None:
            raise RuntimeError("operator_manifests configuration missing in container.yaml")

        repo_dir = os.path.realpath(self.workflow.source.path)
        manifests_rel_path = self.user_config["manifests_dir"]

        manifests_dir = os.path.realpath(os.path.join(repo_dir, manifests_rel_path))
        if not manifests_dir.startswith(repo_dir):
            raise RuntimeError("manifests_dir points outside of cloned repository")

        self.log.info("Looking for operator CSV files in %s", manifests_dir)
        operator_manifest = OperatorManifest.from_directory(manifests_dir)

        if operator_manifest.files:
            path_lines = "\n".join(f.path for f in operator_manifest.files)
            self.log.info("Found operator CSV files:\n%s", path_lines)
        else:
            self.log.info("No operator CSV files found")

        return operator_manifest

    def _get_pullspecs(self, operator_manifest):
        self.log.info("Looking for pullspecs in operator CSV files")
        pullspec_set = set()

        for operator_csv in operator_manifest.files:
            if not operator_csv.has_related_images():
                self.log.info("Getting pullspecs from %s", operator_csv.path)
                pullspec_set.update(operator_csv.get_pullspecs())
            elif operator_csv.has_related_image_envs():
                msg = ("Both relatedImages and RELATED_IMAGE_* env vars present in {}. "
                       "Please remove the relatedImages section, it will be reconstructed "
                       "automatically.".format(operator_csv.path))
                raise RuntimeError(msg)
            else:
                self.log.warning("%s has a relatedImages section, skipping", operator_csv.path)

        # Make sure pullspecs are handled in a deterministic order
        # ImageName does not implement ordering, use str() as key for sorting
        pullspecs = sorted(pullspec_set, key=str)

        if pullspecs:
            pullspec_lines = "\n".join(image.to_str() for image in pullspecs)
            self.log.info("Found pullspecs:\n%s", pullspec_lines)
        else:
            self.log.info("No pullspecs found")

        return pullspecs

    def _get_replacement_pullspecs(self, pullspecs):
        self.log.info("Computing replacement pullspecs")

        pin_digest, replace_repo, replace_registry = self._are_features_enabled()
        if not any([pin_digest, replace_repo, replace_registry]):
            self.log.warning("All replacement features disabled, skipping")
            return {}

        replacer = PullspecReplacer(user_config=self.user_config, site_config=self.site_config)

        for p in pullspecs:
            if not replacer.registry_is_allowed(p):
                raise RuntimeError("Registry not allowed: {} (in {})".format(p.registry, p))

        replacements = {}

        for original in pullspecs:
            self.log.info("Computing replacement for %s", original)
            replaced = original

            if pin_digest:
                self.log.debug("Making sure tag is manifest list digest")
                replaced = replacer.pin_digest(original)

            if replace_repo:
                self.log.debug("Replacing namespace/repo")
                replaced = replacer.replace_repo(replaced)

            if replace_registry:
                self.log.debug("Replacing registry")
                replaced = replacer.replace_registry(replaced)

            self.log.info("Final pullspec: %s", replaced)

            if replaced != original:
                replacements[original] = replaced

        replacement_lines = "\n".join(
            "{} -> {}".format(p, replacements[p]) if p in replacements
            else "{} - no change".format(p)
            for p in pullspecs
        )
        self.log.info("To be replaced:\n%s", replacement_lines)

        return replacements

    def _are_features_enabled(self):
        pin_digest = self.user_config.get("enable_digest_pinning", True)
        replace_repo = self.user_config.get("enable_repo_replacements", True)
        replace_registry = self.user_config.get("enable_registry_replacements", True)

        if not pin_digest:
            self.log.warning("User disabled digest pinning")
        if not replace_repo:
            self.log.warning("User disabled repo replacements")
        if not replace_registry:
            self.log.warning("User disabled registry replacements")

        return pin_digest, replace_repo, replace_registry

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

        self.package_mapping_urls = {
            mapping["registry"]: mapping["package_mappings_url"]
            for mapping in site_config.get("repo_replacements", [])
        }
        # Mapping of [url => package mapping]
        # Loaded when needed, see _get_site_mapping
        self.url_package_mappings = {}

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
            raise RuntimeError("Replacement not configured for package {} (from {}). "
                               "Please specify replacement in {}"
                               .format(package, image, REPO_CONTAINER_CONFIG))
        elif len(replacements) > 1:
            options = ", ".join(replacements)
            raise RuntimeError("Multiple replacements for package {} (from {}): {}. "
                               "Please specify replacement in {}"
                               .format(package, image, options, REPO_CONTAINER_CONFIG))

        self.log.debug("Replacement for package %s: %s", package, replacements[0])
        replacement = ImageName.parse(replacements[0])
        return self._replace(image, namespace=replacement.namespace, repo=replacement.repo)

    def _get_site_mapping(self, registry):
        """
        Get the package mapping file for the given registry. If said file has
        not yet been read, read it and save mapping for later. Return mapping.
        """
        mapping_url = self.package_mapping_urls.get(registry)

        if mapping_url is None:
            return None
        elif mapping_url in self.url_package_mappings:
            return self.url_package_mappings[mapping_url]

        self.log.debug("Downloading mapping file for %s from %s", registry, mapping_url)
        mapping = read_yaml_from_url(mapping_url, "schemas/package_mapping.json")
        self.url_package_mappings[mapping_url] = mapping
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
