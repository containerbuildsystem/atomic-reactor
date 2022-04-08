"""
Copyright (c) 2017-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import logging
from typing import Iterator, List

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.types import RpmComponent
from atomic_reactor.util import is_scratch_build
from atomic_reactor.constants import PLUGIN_COMPARE_COMPONENTS_KEY


T_RPM = "rpm"
SUPPORTED_TYPES = (T_RPM,)


def filter_components_by_name(name, components_list, type_=T_RPM) -> Iterator[RpmComponent]:
    """Generator filters components from components_list by name"""
    for components in components_list:
        for component in components:
            if component['type'] == type_ and component['name'] == name:
                yield component


class CompareComponentsPlugin(PostBuildPlugin):
    """
    Compare components from each platform build and verify the same version was
    in each platform build.
    """

    key = PLUGIN_COMPARE_COMPONENTS_KEY
    is_allowed_to_fail = False

    def rpm_compare(self, a, b) -> None:
        """
        Compare rpm component version 'a' with 'b'.
        'name' is implied equal as it is the key() lookup
        """
        if a['version'] == b['version'] and \
           a['release'] == b['release'] and \
           a['signature'] == b['signature']:
            return

        raise ValueError("%s != %s" % (a, b))

    def get_rpm_components_list(self) -> List[List[RpmComponent]]:
        """
        Get the rpm components list for each platform build and
        merge it in one list for comparison.

        :return: list of component lists
        """
        comp_list = []

        for components in self.workflow.data.image_components.values():
            comp_list.append(components)

        return comp_list

    def log_rpm_component(self, component, loglevel=logging.WARNING) -> None:
        assert component['type'] == T_RPM
        self.log.log(
            loglevel,
            "%s: %s-%s-%s (%s)",  # platform: name-version-release (signature)
            component['arch'], component['name'], component['version'],
            component['release'], component['signature']
        )

    def run(self) -> None:
        """
        Run the plugin.
        """
        if is_scratch_build(self.workflow):
            # scratch build is testing build, which may contain different component
            # version for different arches
            self.log.info('scratch build, skipping plugin')
            return

        wf_data = self.workflow.data

        if wf_data.dockerfile_images.base_from_scratch:
            self.log.info("Skipping comparing components: unsupported for FROM-scratch images")
            return

        comp_list = self.get_rpm_components_list()

        if not comp_list:
            raise ValueError("No components to compare")

        package_comparison_exceptions = self.workflow.conf.package_comparison_exceptions

        # master compare list
        master_comp = {}

        # The basic strategy is to start with empty lists and add new component
        # versions as we find them.  Upon next iteration, we should notice
        # duplicates and be able to compare them.  If the match fails, we raise
        # an exception.  If the component name does not exist, assume it was an
        # arch dependency, add it to list and continue.  By the time we get to
        # the last arch, we should have every possible component in the master
        # list to compare with.

        # Keep everything separated by component type
        failed_components = set()
        for components in comp_list:
            for component in components:
                t = component['type']
                name = str(component['name'])  # cast to str to avoid mypy ambiguity

                if name in package_comparison_exceptions:
                    self.log.info("Ignoring comparison of package %s", name)
                    continue

                if t not in SUPPORTED_TYPES:
                    raise ValueError("Type %s not supported" % t)

                if name in failed_components:
                    # report a failed component only once
                    continue

                identifier = (t, name)
                if identifier not in master_comp:
                    master_comp[identifier] = component
                    continue

                if t == T_RPM:
                    mc = master_comp[identifier]
                    try:
                        self.rpm_compare(mc, component)
                    except ValueError as ex:
                        self.log.debug("Mismatch details: %s", ex)
                        self.log.warning(
                            "Comparison mismatch for component %s:", name)

                        # use all components to provide complete list
                        for comp in filter_components_by_name(name, comp_list):
                            self.log_rpm_component(comp)
                        failed_components.add(name)

        if failed_components:
            raise ValueError(
                "Failed component comparison for components: "
                "{components}".format(
                    components=', '.join(sorted(failed_components))
                )
            )
