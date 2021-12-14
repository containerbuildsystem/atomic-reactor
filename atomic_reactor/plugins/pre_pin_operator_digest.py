"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import logging

from osbs.utils import Labels, ImageName

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.constants import (
    PLUGIN_PIN_OPERATOR_DIGESTS_KEY,
    INSPECT_CONFIG,
    REPO_CONTAINER_CONFIG,
)
from atomic_reactor.util import (RegistrySession,
                                 RegistryClient,
                                 has_operator_bundle_manifest,
                                 read_yaml_from_url, df_parser,
                                 terminal_key_paths,
                                 map_to_user_params)
from osbs.utils.yaml import (
    load_schema,
    validate_with_schema,
)
from atomic_reactor.utils.operator import OperatorManifest
from atomic_reactor.utils.retries import get_retrying_requests_session


class PinOperatorDigestsPlugin(PreBuildPlugin):
    """
    Plugin runs for operator manifest bundle builds.

    - finds container pullspecs in operator ClusterServiceVersion files
    - computes replacement pullspecs:
        - replaces tags with manifest list digests
        - replaces repos (and namespaces) based on operator_manifests.repo_replacements
          configuration in container.yaml and r-c-m*
        - replaces registries based on operator_manifests.registry_post_replace in r-c-m*
    - replaces pullspecs in ClusterServiceVersion files based on replacement pullspec mapping
    - creates relatedImages sections in ClusterServiceVersion files

    Files that already have a relatedImages section are excluded.

    * reactor-config-map
    """

    key = PLUGIN_PIN_OPERATOR_DIGESTS_KEY
    is_allowed_to_fail = False

    args_from_user_params = map_to_user_params(
        "operator_csv_modifications_url",
    )

    def __init__(self, workflow, operator_csv_modifications_url=None):
        """
        Initialize pin_operator_digests plugin

        :param workflow: DockerBuildWorkflow instance
        :param operator_csv_modifications_url: str, URL to JSON file with operator
                                      CSV modifications
        """
        super(PinOperatorDigestsPlugin, self).__init__(workflow)
        self.user_config = workflow.source.config.operator_manifests
        self.operator_csv_modifications_url = operator_csv_modifications_url

        site_config = self.workflow.conf.operator_manifests
        self.operator_csv_modification_allowed_attributes = set(
            tuple(key_path)
            for key_path in (
                site_config
                .get('csv_modifications', {})
                .get('allowed_attributes', [])
            )
        )

    def _validate_operator_csv_modifications_schema(self, modifications):
        """Validate if provided operator CSV modification are valid according schema"""
        schema = load_schema(
            'atomic_reactor',
            'schemas/operator_csv_modifications.json'
        )
        validate_with_schema(modifications, schema)

    def _validate_operator_csv_modifications_duplicated_images(self, modifications):
        """Validate if provided operator CSV modifications doesn't provide duplicated entries"""
        original_pullspecs = set()
        duplicated = set()
        for repl in modifications.get('pullspec_replacements', ()):
            pullspec = ImageName.parse(repl['original'])
            if pullspec in original_pullspecs:
                duplicated.add(pullspec)
                self.log.error(
                    "Operator CSV modifications contains duplicated "
                    "original replacement pullspec %s", pullspec)
            original_pullspecs.add(pullspec)
        if duplicated:
            raise RuntimeError(
                f"Provided CSV modifications contain duplicated "
                f"original entries in pullspec_replacement: "
                f"{', '.join(sorted(str(dup) for dup in duplicated))}"
            )

    def _validate_operator_csv_modifications_allowed_keys(self, modifications):
        """Validate if used attributes in update/append are allowed to be modified"""
        allowed_attrs = self.operator_csv_modification_allowed_attributes

        def to_str(path):
            return '.'.join(part.replace('.', r'\.') for part in path)

        def validate(mods):
            not_allowed = []
            for key_path in terminal_key_paths(mods):
                if key_path not in allowed_attrs:
                    self.log.error(
                        "Operator CSV attribute %s is not allowed to be modified",
                        key_path
                    )
                    not_allowed.append(key_path)

            if not_allowed:
                raise RuntimeError(
                    f"Operator CSV attributes: {', '.join(to_str(attr) for attr in not_allowed)}; "
                    f"are not allowed to be modified "
                    f"by service configuration. Attributes allowed for modification "
                    f"are: {', '.join(to_str(attr) for attr in allowed_attrs) or 'N/A'}"
                )

        validate(modifications.get('update', {}))
        validate(modifications.get('append', {}))

    def _validate_operator_csv_modifications(self, modifications):
        """Validate if provided operator CSV modification correct"""
        self._validate_operator_csv_modifications_schema(modifications)
        self._validate_operator_csv_modifications_duplicated_images(modifications)
        self._validate_operator_csv_modifications_allowed_keys(modifications)

    def _fetch_operator_csv_modifications(self):
        """Fetch operator CSV modifications"""

        if not self.operator_csv_modifications_url:
            return None

        session = get_retrying_requests_session()

        self.log.info(
            "Fetching operator CSV modifications data from %s",
            self.operator_csv_modifications_url
        )
        resp = session.get(self.operator_csv_modifications_url)
        try:
            resp.raise_for_status()
        except Exception as exc:
            raise RuntimeError(
                f"Failed to fetch the operator CSV modification JSON "
                f"from {self.operator_csv_modifications_url}: {exc}"
            ) from exc

        try:
            csv_modifications = resp.json()
        except Exception as exc:
            # catching raw Exception because requests uses various json decoders
            # in different versions
            raise RuntimeError(
                f"Failed to parse operator CSV modification JSON "
                f"from {self.operator_csv_modifications_url}: {exc}"
            ) from exc

        self.log.info("Operator CSV modifications: %s", csv_modifications)

        self._validate_operator_csv_modifications(csv_modifications)
        return csv_modifications

    def run(self):
        """
        Run pin_operator_digest plugin
        """
        if not self.should_run():
            return

        if self.operator_csv_modifications_url:
            self.log.info(
                "Operator CSV modification URL specified: %s",
                self.operator_csv_modifications_url
            )

        operator_manifest = self._get_operator_manifest()
        replacement_pullspecs = {}

        related_images_metadata = {
            'pullspecs': [],
            'created_by_osbs': True,
        }
        operator_manifests_metadata = {
            'related_images': related_images_metadata,
            'custom_csv_modifications_applied': bool(self.operator_csv_modifications_url)
        }

        should_skip = self._skip_all()

        if should_skip:
            self.log.warning("skip_all defined for operator manifests")

        pullspecs = self._get_pullspecs(operator_manifest.csv, should_skip)

        if operator_manifest.csv.has_related_images() or should_skip:
            if self.operator_csv_modifications_url:
                raise RuntimeError(
                    "OSBS cannot modify operator CSV file because this operator bundle "
                    "is managed by owner (digest pinning explicitly disabled or "
                    "RelatedImages section in CSV exists)"
                )

            # related images already exists
            related_images_metadata['created_by_osbs'] = False
            related_images_metadata['pullspecs'] = [{
                'original': item,
                'new': item,
                'pinned': False,
                'replaced': False,
            } for item in pullspecs]
        else:
            if pullspecs:
                replacement_pullspecs = self._get_replacement_pullspecs(pullspecs)
                related_images_metadata['pullspecs'] = replacement_pullspecs
            else:
                # no pullspecs don't create relatedImages section
                related_images_metadata['created_by_osbs'] = False

        if should_skip:
            return operator_manifests_metadata

        replacement_pullspecs = {
            repl['original']: repl['new']
            for repl in replacement_pullspecs
            if repl['replaced']
        }

        operator_csv = operator_manifest.csv

        self.log.info("Updating operator CSV file")
        if not operator_csv.has_related_images():
            self.log.info("Replacing pullspecs in %s", operator_csv.path)
            # Replace pullspecs everywhere, not just in locations in which they
            # are expected to be found - OCP 4.4 workaround
            operator_csv.replace_pullspecs_everywhere(replacement_pullspecs)

            self.log.info("Creating relatedImages section in %s", operator_csv.path)
            operator_csv.set_related_images()

            operator_csv_modifications = self._fetch_operator_csv_modifications()
            if operator_csv_modifications:
                operator_csv.modifications_append(
                    operator_csv_modifications.get('append', {}))
                operator_csv.modifications_update(
                    operator_csv_modifications.get('update', {}))

            operator_csv.dump()
        else:
            self.log.warning("%s has a relatedImages section, skipping", operator_csv.path)

        return operator_manifests_metadata

    def should_run(self):
        """
        Determine if this is an operator manifest bundle build

        :return: bool, should plugin run?
        """
        if not has_operator_bundle_manifest(self.workflow):
            self.log.info("Not an operator manifest bundle build, skipping plugin")
            return False
        if not self.workflow.conf.operator_manifests:
            msg = "operator_manifests configuration missing in reactor config map, aborting"
            self.log.warning(msg)
            return False
        return True

    def _skip_all(self):
        skip_all = self.user_config.get("skip_all", False)

        if not skip_all:
            return False

        site_config = self.workflow.conf.operator_manifests
        allowed_packages = site_config.get("skip_all_allow_list", [])

        parser = df_parser(self.workflow.df_path, workflow=self.workflow)
        dockerfile_labels = parser.labels
        labels = Labels(dockerfile_labels)

        component_label = labels.get_name(Labels.LABEL_TYPE_COMPONENT)
        component = dockerfile_labels[component_label]

        if component in allowed_packages:
            return True
        else:
            raise RuntimeError("Koji package: {} isn't allowed to use skip_all for operator "
                               "bundles".format(component))

    def _get_operator_manifest(self):
        manifests_dir = self.workflow.source.manifests_dir
        self.log.info("Looking for operator CSV files in %s", manifests_dir)
        operator_manifest = OperatorManifest.from_directory(manifests_dir)
        self.log.info("Found operator CSV file: %s", operator_manifest.csv.path)

        return operator_manifest

    def _get_pullspecs(self, operator_csv, skip_all):
        """Get pullspecs from CSV file

        :param OperatorCSV operator_csv: a cluster service version (CSV) file
            from where to find out pullspecs.
        :return: a list of pullspecs sorted by each one's string representation.
            If CSV does not have spec.relatedImages, all pullspecs will be
            found out from all possible locations. If CSV has spec.relatedImages,
            return the pullspecs contained.
        :rtype: list[ImageName]
        :raises RuntimeError: if the CSV has both spec.relatedImages and
            pullspecs referenced by environment variables prefixed with
            RELATED_IMAGE_.
        """
        self.log.info("Looking for pullspecs in operator CSV file")
        pullspec_set = set()

        if not operator_csv.has_related_images():
            if skip_all and operator_csv.get_pullspecs():
                raise RuntimeError("skip_all defined but relatedImages section doesn't exist")

            self.log.info("Getting pullspecs from %s", operator_csv.path)
            pullspec_set.update(operator_csv.get_pullspecs())
        elif operator_csv.has_related_image_envs():
            msg = ("Both relatedImages and RELATED_IMAGE_* env vars present in {}. "
                   "Please remove the relatedImages section, it will be reconstructed "
                   "automatically.".format(operator_csv.path))

            if not skip_all:
                raise RuntimeError(msg)
        else:
            pullspec_set.update(operator_csv.get_related_image_pullspecs())

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
        """Replace components of pullspecs

        :param pullspecs: a list of pullspecs.
        :type pullspecs: list[ImageName]
        :return: a list of replacement result. Each of the replacement result
            is a mapping containing key/value pairs:

            * ``original``: ImageName, the original pullspec.
            * ``new``: ImageName, the replaced/non-replaced pullspec.
            * ``pinned``: bool, indicate whether the tag is replaced with a
                          specific digest.
            * ``replaced``: bool, indicate whether the new pullspec has change
                            of repository or registry.

        :rtype: list[dict[str, ImageName or bool]]
        :raises RuntimeError: if pullspecs cannot be properly replaced
        """
        if self.operator_csv_modifications_url:
            replacements = self._get_replacement_pullspecs_from_csv_modifications(pullspecs)
        else:
            replacements = self._get_replacement_pullspecs_OSBS_resolution(pullspecs)

        replacement_lines = "\n".join(
            "{original} -> {new}".format(**r) if r['replaced']
            else "{original} - no change".format(**r)
            for r in replacements
        )
        self.log.info("To be replaced:\n%s", replacement_lines)

        return replacements

    def _get_replacement_pullspecs_from_csv_modifications(self, pullspecs):
        """Replace components of pullspecs based on externally provided CSV modifications

        :param pullspecs: a list of pullspecs.
        :type pullspecs: list[ImageName]
        :return: a list of replacement result. Each of the replacement result
            is a mapping containing key/value pairs:

            * ``original``: ImageName, the original pullspec.
            * ``new``: ImageName, the replaced/non-replaced pullspec.
            * ``pinned``: bool, indicate whether the tag is replaced with a
                          specific digest.
            * ``replaced``: bool, indicate whether the new pullspec has change
                            of repository or registry.

        :rtype: list[dict[str, ImageName or bool]]
        :raises RuntimeError: if provided CSV modification doesn't contain all
                              required pullspecs or contain different ones
        """
        operator_csv_modifications = self._fetch_operator_csv_modifications()
        mod_pullspec_repl = operator_csv_modifications.get('pullspec_replacements', [])

        # check if modification data contains all required pullspecs
        pullspecs_set = set(pullspecs)
        mod_pullspecs_set = set((ImageName.parse(p['original']) for p in mod_pullspec_repl))

        missing = pullspecs_set - mod_pullspecs_set
        if missing:
            raise RuntimeError(
                f"Provided operator CSV modifications misses following pullspecs: "
                f"{', '.join(sorted(str(p) for p in missing))}"
            )

        extra = mod_pullspecs_set - pullspecs_set
        if extra:
            raise RuntimeError(
                f"Provided operator CSV modifications defines extra pullspecs: "
                f"{','.join(sorted(str(p) for p in extra))}"
            )

        # Copy replacements from provided CSV modifications file, fill missing 'replaced' filed
        replacements = [
            {
                'original': ImageName.parse(repl['original']),
                'new': ImageName.parse(repl['new']),
                'pinned': repl['pinned'],
                'replaced': repl['original'] != repl['new']
            }
            for repl in mod_pullspec_repl
        ]

        return replacements

    def _get_replacement_pullspecs_OSBS_resolution(self, pullspecs):
        """
        Replace components of pullspecs according to operator manifest
        replacement config

        :param pullspecs: a list of pullspecs.
        :type pullspecs: list[ImageName]
        :return: a list of replacement result. Each of the replacement result
            is a mapping containing key/value pairs:

            * ``original``: ImageName, the original pullspec.
            * ``new``: ImageName, the replaced/non-replaced pullspec.
            * ``pinned``: bool, indicate whether the tag is replaced with a
                          specific digest.
            * ``replaced``: bool, indicate whether the new pullspec has change
                            of repository or registry.

        :rtype: list[dict[str, ImageName or bool]]
        :raises RuntimeError: if the registry of a pullspec is not allowed.
            Refer to the ``operator_manifest.allowed_registries`` in atomic
            reactor config.
        """
        self.log.info("Computing replacement pullspecs")

        replacements = []

        pin_digest, replace_repo, replace_registry = self._are_features_enabled()
        if not any([pin_digest, replace_repo, replace_registry]):
            self.log.warning("All replacement features disabled")

        replacer = PullspecReplacer(user_config=self.user_config, workflow=self.workflow)

        for p in pullspecs:
            if not replacer.registry_is_allowed(p):
                raise RuntimeError("Registry not allowed: {} (in {})".format(p.registry, p))

        for original in pullspecs:
            self.log.info("Computing replacement for %s", original)
            replaced = original
            pinned = False

            if pin_digest:
                self.log.debug("Making sure tag is manifest list digest")
                replaced = replacer.pin_digest(original)
                if replaced != original:
                    pinned = True

            if replace_repo:
                self.log.debug("Replacing namespace/repo")
                replaced = replacer.replace_repo(replaced)

            if replace_registry:
                self.log.debug("Replacing registry")
                replaced = replacer.replace_registry(replaced)

            self.log.info("Final pullspec: %s", replaced)

            replacements.append({
                'original': original,
                'new': replaced,
                'pinned': pinned,
                'replaced': replaced != original
            })

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


_KEEP = object()


class PullspecReplacer(object):
    """
    Helper that takes care of replacing parts of image pullspecs
    """

    def __init__(self, user_config, workflow):
        """
        Initialize a PullspecReplacer

        :param user_config: container.yaml operator_manifest configuration
        :param workflow: DockerBuildWorkflow, contains reactor config map
        """
        log_name = "atomic_reactor.plugins.{}".format(PinOperatorDigestsPlugin.key)
        self.log = logging.getLogger(log_name)

        self.workflow = workflow
        site_config = self.workflow.conf.operator_manifests

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

        # RegistryClient instances cached by registry name
        self.registry_clients = {}

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
        registry_client = self._get_registry_client(image.registry)
        digest = registry_client.get_manifest_list_digest(image)
        return self._replace(image, tag=digest)

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
        registry_client = self._get_registry_client(image.registry)
        inspect = registry_client.get_inspect_for_image(image)
        labels = Labels(inspect[INSPECT_CONFIG].get("Labels", {}))

        try:
            _, package = labels.get_name_and_value(Labels.LABEL_TYPE_COMPONENT)
            self.log.debug("Resolved package name: %s", package)
        except KeyError as exc:
            raise RuntimeError("Image has no component label: {}".format(image)) from exc

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

    def _get_registry_client(self, registry):
        """
        Get registry client for specified registry, cached by registry name
        """
        client = self.registry_clients.get(registry)
        if client is None:
            session = RegistrySession.create_from_config(self.workflow.conf, registry=registry)
            client = RegistryClient(session)
            self.registry_clients[registry] = client
        return client

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
