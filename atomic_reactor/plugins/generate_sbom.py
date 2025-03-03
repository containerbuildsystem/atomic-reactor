"""
Copyright (c) 2023 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import json
import os
import subprocess
import tempfile
from copy import deepcopy
from typing import Any, Dict, List, Optional

from atomic_reactor.constants import (PLUGIN_GENERATE_SBOM,
                                      PLUGIN_HERMETO_POSTPROCESS,
                                      PLUGIN_RPMQA,
                                      PLUGIN_RESOLVE_REMOTE_SOURCE,
                                      SBOM_SCHEMA_PATH,
                                      PLUGIN_FETCH_MAVEN_KEY,
                                      INSPECT_CONFIG,
                                      KOJI_BTYPE_ICM,
                                      ICM_JSON_FILENAME,
                                      HERMETO_BUILD_DIR)
from atomic_reactor.config import get_cachito_session, get_koji_session
from atomic_reactor.utils import retries
from atomic_reactor.utils.cachito import CachitoAPI
from atomic_reactor.plugin import Plugin
from atomic_reactor.util import (read_fetch_artifacts_url, read_fetch_artifacts_koji,
                                 base_image_is_custom, get_retrying_requests_session,
                                 validate_with_schema, get_platforms)

from osbs.utils import Labels
import koji


class GenerateSbomPlugin(Plugin):
    """
    Construct full SBOM from different plugins results,
    for rpms, remote sources, pnc artifacts

    SBOM example:

    {
      "bomFormat": "CycloneDX",
      "specVersion": "1.4",
      "version": 1,
      "components": [
        {
          "name": "npm-without-deps",
          "type": "library",
          "version": "1.0.0",
          "purl": "pkg:github/testing/npm-without-deps@2f0ce1d7b1f8b35572d919428b965285a69583f6",
          "build_dependency": False
        },
        {
          "name": "fmt",
          "type": "library",
          "purl": "pkg:golang/fmt",
          "build_dependency": True
        },
        {
          "name": "yarn-without-deps",
          "type": "library",
          "version": "1.0.0",
          "purl": "pkg:github/testing/yarn-without-deps@da0a2888aa7aab37fec34c0b36d9e44560d2cf3e",
          "build_dependency": False
        }
      ]
      "incompleteness_reasons": [
        {"type": "other", "description": "fetch url is used"},
        {"type": "other", "description": "fetch koji is used"},
        {"type": "other", "description": "lookaside cache is used"},
        {"type": "other", "description": "parent build 'NVR' is missing SBOM"}
      ]
    }
    """
    key = PLUGIN_GENERATE_SBOM
    is_allowed_to_fail = False
    minimal_sbom = {
        'bomFormat': 'CycloneDX',
        'specVersion': '1.4',
        'version': 1,
        'components': [],
    }

    def __init__(self, workflow):
        """
        :param workflow: DockerBuildWorkflow instance
        """
        super(GenerateSbomPlugin, self).__init__(workflow)
        wf_data = self.workflow.data

        remote_source_results = wf_data.plugins_results.get(PLUGIN_RESOLVE_REMOTE_SOURCE) or []
        self.remote_source_ids = [remote_source['id'] for remote_source in remote_source_results]

        self.hermeto_remote_sources = wf_data.plugins_results.get(PLUGIN_HERMETO_POSTPROCESS) or []

        self.rpm_components = wf_data.plugins_results.get(PLUGIN_RPMQA) or {}

        fetch_maven_results = wf_data.plugins_results.get(PLUGIN_FETCH_MAVEN_KEY) or {}
        self.pnc_components = fetch_maven_results.get('sbom_components') or []

        self.incompleteness_reasons = set()
        self.sbom: Dict[str, Any] = {}

        self.koji_session = get_koji_session(self.workflow.conf)
        self.pathinfo = self.workflow.conf.koji_path_info

        self.req_session = get_retrying_requests_session()
        self.df_images = self.workflow.data.dockerfile_images

        self.all_platforms = get_platforms(self.workflow.data)

    @property
    def cachito_session(self) -> CachitoAPI:
        if not self.workflow.conf.cachito:
            raise RuntimeError('No Cachito configuration defined')
        return get_cachito_session(self.workflow.conf)

    def lookaside_cache_check(self) -> None:
        """check if lookaside cache was used and add it to incompleteness reasons"""
        source_path = self.workflow.source.get()
        sources_cache_file = os.path.join(source_path, 'sources')

        if os.path.exists(sources_cache_file):
            if os.path.getsize(sources_cache_file) > 0:
                self.incompleteness_reasons.add("lookaside cache is used")

    def fetch_url_or_koji_check(self) -> None:
        """check if fetch url or koji was used and add it to incompleteness reasons"""
        if read_fetch_artifacts_koji(self.workflow):
            self.incompleteness_reasons.add("fetch koji is used")

        if read_fetch_artifacts_url(self.workflow):
            self.incompleteness_reasons.add("fetch url is used")

    def get_hermeto_sbom(self) -> dict:
        """Get SBOM from Hermeto results"""
        global_sbom_path = self.workflow.build_dir.path/HERMETO_BUILD_DIR/"bom.json"
        with open(global_sbom_path, "r") as f:
            return json.load(f)

    def add_parent_missing_sbom_reason(self, nvr: str) -> None:
        self.incompleteness_reasons.add(f"parent build '{nvr}' is missing SBOM")

    def detect_parent_image_nvr(self, image_name: str,
                                inspect_data: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """
        Look for the NVR labels, if any, in the image.

        :return NVR string if labels found, otherwise None
        """

        if inspect_data is None:
            # Inspect any platform: the N-V-R labels should be equal for all platforms
            inspect_data = self.workflow.imageutil.get_inspect_for_image(image_name)
        labels = Labels(inspect_data[INSPECT_CONFIG].get('Labels', {}))

        label_names = [Labels.LABEL_TYPE_COMPONENT, Labels.LABEL_TYPE_VERSION,
                       Labels.LABEL_TYPE_RELEASE]
        label_values = []

        for lbl_name in label_names:
            try:
                _, lbl_value = labels.get_name_and_value(lbl_name)
                label_values.append(lbl_value)
            except KeyError:
                self.log.warning("Failed to find label '%s' in parent image '%s'.",
                                 labels.get_name(lbl_name), image_name)

        if len(label_values) != len(label_names):  # don't have all the necessary labels
            self.log.warning("Image '%s' NVR missing; not searching for Koji build.", image_name)
            return None

        return '-'.join(label_values)

    def check_build_state(self, build: Dict[str, Any], nvr: str) -> bool:
        if build:
            build_state = koji.BUILD_STATES[build['state']]
            if build_state != 'COMPLETE':
                self.add_parent_missing_sbom_reason(nvr)
                return False
        else:
            self.add_parent_missing_sbom_reason(nvr)
            return False
        return True

    def get_sbom_json(self, sbom_url: str) -> Dict[str, Any]:
        resp = self.req_session.get(sbom_url)
        resp.raise_for_status()

        try:
            return(resp.json())
        except Exception as exc:
            msg = (f'JSON data is expected from {sbom_url}, '
                   f'but the response contains: {resp.content}.')
            raise ValueError(msg) from exc

    def get_sbom_urls_from_build(self, build: Dict[str, Any]) -> Dict[Any, str]:
        sbom_urls = {}

        archives = self.koji_session.listArchives(build['build_id'], type=KOJI_BTYPE_ICM)
        sbom_path = self.pathinfo.typedir(build, btype=KOJI_BTYPE_ICM)

        icm_filenames = {ICM_JSON_FILENAME.format(plat): plat for plat in self.all_platforms}
        for archive in archives:
            if archive['filename'] in icm_filenames:
                platform = icm_filenames[archive['filename']]
                sbom_urls[platform] = os.path.join(sbom_path, archive['filename'])

        if sbom_urls:
            if set(self.all_platforms) != set(sbom_urls.keys()):
                msg = f"build '{build['build_id']}', doesn't have icm for all " \
                      f"platforms '{self.all_platforms}'"
                raise RuntimeError(msg)

        return sbom_urls

    def get_parent_images_nvr(self) -> List[Optional[str]]:
        parent_images_nvr = []
        for img, local_tag in self.df_images.items():
            img_str = img.to_str()
            if base_image_is_custom(img_str):
                continue

            nvr = self.detect_parent_image_nvr(local_tag) if local_tag else None
            parent_images_nvr.append(nvr)
        return parent_images_nvr

    def get_parent_image_components(self, nvr: Optional[str]) -> Dict[Any, Any]:
        parent_components = {}

        if not nvr:
            self.incompleteness_reasons.add('parent build is missing SBOM')
            return {}

        build = self.koji_session.getBuild(nvr)

        if self.check_build_state(build, nvr):
            sbom_urls = self.get_sbom_urls_from_build(build)

            if sbom_urls:
                for platform in self.all_platforms:
                    parent_image_sbom_json = self.get_sbom_json(sbom_urls[platform])
                    parent_components[platform] = parent_image_sbom_json['components']

                    # add reasons from parent images
                    for reason in parent_image_sbom_json.get('incompleteness_reasons', {}):
                        self.incompleteness_reasons.add(reason.get('description'))
            else:
                self.add_parent_missing_sbom_reason(nvr)

        if not parent_components:
            return parent_components

        for platform in self.all_platforms:
            for component in parent_components[platform]:
                component.pop('build_dependency', None)

        return parent_components

    def get_parents_unique_components(
            self, parent_images_nvrs: List[Optional[str]]) -> Dict[Any, List[Dict[str, Any]]]:
        all_parents_components: Dict[Any, List[Dict[str, Any]]] = {}
        for platform in self.all_platforms:
            all_parents_components[platform] = []

        for nvr in set(parent_images_nvrs):
            parents_components = self.get_parent_image_components(nvr)

            if parents_components:
                for platform in self.all_platforms:
                    all_parents_components[platform].extend(parents_components[platform])

        # sort indirect components and add build_dependency True
        for platform in self.all_platforms:
            unique_components = \
                self.get_unique_and_sorted_components(all_parents_components[platform], True)
            all_parents_components[platform] = unique_components

        return all_parents_components

    def get_unique_and_sorted_components(
            self, components: List[Dict[str, Any]],
            build_dependency: Optional[bool] = None) -> List[Dict[str, Any]]:
        unique_components: List[Dict[str, Any]] = []

        if not components:
            return unique_components

        components.sort(key=lambda c: (c["purl"], c["name"], c.get("version")))

        for component in components:
            if build_dependency is not None:
                component['build_dependency'] = build_dependency
            if not unique_components or component != unique_components[-1]:
                unique_components.append(component)

        return unique_components

    def push_sboms_to_registry(self):
        docker_config = os.path.join(self.workflow.conf.registries_cfg_path, '.dockerconfigjson')

        if not os.path.exists(docker_config):
            self.log.warning("Dockerconfig json doesn't exist in : '%s'",  docker_config)
            return

        tmpdir = tempfile.mkdtemp()
        os.environ["DOCKER_CONFIG"] = tmpdir
        sbom_type = '--type=cyclonedx'
        dest_config = os.path.join(tmpdir, 'config.json')
        # cosign requires docker config named exactly 'config.json'
        os.symlink(docker_config, dest_config)

        for platform in self.all_platforms:
            image = self.workflow.data.tag_conf.get_unique_images_with_platform(platform)[0]
            sbom_file_path = os.path.join(tmpdir, f"icm-{platform}.json")
            sbom_param = f"--sbom={sbom_file_path}"
            cmd = ["cosign", "attach", "sbom", image.to_str(), sbom_type,  sbom_param]

            with open(sbom_file_path, 'w') as outfile:
                json.dump(self.sbom[platform], outfile, indent=4, sort_keys=True)
            self.log.debug('SBOM JSON saved to: %s', sbom_file_path)

            try:
                self.log.info('pushing SBOM for platform %s to registry', platform)
                retries.run_cmd(cmd)
            except subprocess.CalledProcessError as e:
                self.log.error("SBOM push for platform %s failed with output:\n%s",
                               platform, e.output)
                raise

    def run(self) -> Dict[str, Any]:
        """Run the plugin."""
        self.lookaside_cache_check()
        self.fetch_url_or_koji_check()

        remote_souces_components = []

        if self.remote_source_ids:
            remote_sources_sbom = self.cachito_session.get_sbom(self.remote_source_ids)
            remote_souces_components = remote_sources_sbom['components']
        elif self.hermeto_remote_sources:
            # Hermeto and Cachito are not supported to be used together
            remote_souces_components = self.get_hermeto_sbom()['components']

        # add components from cachito, rpms, pnc
        for platform in self.all_platforms:
            self.sbom[platform] = deepcopy(self.minimal_sbom)
            self.sbom[platform]['components'].extend(deepcopy(remote_souces_components))
            if self.rpm_components:
                self.sbom[platform]['components'].extend(deepcopy(self.rpm_components[platform]))
            self.sbom[platform]['components'].extend(deepcopy(self.pnc_components))

        # get nvrs for all parent images
        parent_images_nvrs = self.get_parent_images_nvr()
        self.log.debug('parent nvrs "%s"', parent_images_nvrs)

        base_image_components = None

        if not (self.df_images.base_from_scratch or self.df_images.custom_base_image):
            base_image_nvr = parent_images_nvrs.pop(0)
            base_image_components = self.get_parent_image_components(base_image_nvr)

        # add components from base image
        if base_image_components:
            for platform in self.all_platforms:
                self.sbom[platform]['components'].extend(base_image_components[platform])

        # sort direct components, we need this to make components unique to pass validation
        for platform in self.all_platforms:
            unique_components = \
                self.get_unique_and_sorted_components(self.sbom[platform]['components'], None)
            self.sbom[platform]['components'] = unique_components

            # validate sbom with schema
            # validating only what we got new, because sboms from parent images were already
            # verified during their builds
            validate_with_schema(self.sbom[platform], SBOM_SCHEMA_PATH)

            # sort direct components and add build_dependency False
            unique_components2 = \
                self.get_unique_and_sorted_components(self.sbom[platform]['components'], False)
            self.sbom[platform]['components'] = unique_components2

        # get components for all parent images but base image
        parent_unique_components = self.get_parents_unique_components(parent_images_nvrs)

        for platform in self.all_platforms:
            self.sbom[platform]['components'].extend(parent_unique_components[platform])

        # create unique and sorted incompleteness reasons
        incompleteness_reasons_full = [
            {"type": "other", "description": reason}
            for reason in sorted(self.incompleteness_reasons)
        ]

        for platform in self.all_platforms:
            self.sbom[platform]['incompleteness_reasons'] = incompleteness_reasons_full

        self.push_sboms_to_registry()

        return self.sbom
