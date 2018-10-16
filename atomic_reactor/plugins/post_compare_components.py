"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import logging

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.plugins.pre_reactor_config import get_package_comparison_exceptions
from atomic_reactor.constants import (PLUGIN_COMPARE_COMPONENTS_KEY,
                                      PLUGIN_FETCH_WORKER_METADATA_KEY)


T_RPM = "rpm"
SUPPORTED_TYPES = (T_RPM,)


def filter_components_by_name(name, components_list, type_=T_RPM):
    """Generator filters components from components_list by name"""
    for components in components_list:
        for component in components:
            if component['type'] == type_ and component['name'] == name:
                yield component


class CompareComponentsPlugin(PostBuildPlugin):
    """
    Compare components from each worker build and verify the same version was
    on each worker.
    """

    key = PLUGIN_COMPARE_COMPONENTS_KEY
    is_allowed_to_fail = False

    def rpm_compare(self, a, b):
        """
        Compare rpm component version 'a' with 'b'.
        'name' is implied equal as it is the key() lookup
        """
        if a['version'] == b['version'] and \
           a['release'] == b['release'] and \
           a['signature'] == b['signature']:
            return

        raise ValueError("%s != %s" % (a, b))

    def get_component_list_from_workers(self, worker_metadatas):
        """
        Find the component lists from each worker build.

        The components that are interesting are under the 'output' key.  The
        buildhost's components are ignored.

        Inside the 'output' key are various 'instances'.  The only 'instance'
        with a 'component list' is the 'docker-image' instance.  The 'log'
        instances are ignored for now.

        Reference plugin post_koji_upload for details on how this is created.

        :return: list of component lists
        """
        comp_list = []
        for platform in sorted(worker_metadatas.keys()):
            for instance in worker_metadatas[platform]['output']:
                if instance['type'] == 'docker-image':
                    if 'components' not in instance or not instance['components']:
                        self.log.warn("Missing 'components' key in 'output' metadata instance: %s",
                                      instance)
                        continue

                    comp_list.append(instance['components'])

        return comp_list

    def log_rpm_component(self, component, loglevel=logging.WARNING):
        assert component['type'] == T_RPM
        self.log.log(
            loglevel,
            "%s: %s-%s-%s (%s)",  # platform: name-version-release (signature)
            component['arch'], component['name'], component['version'],
            component['release'], component['signature']
        )

    def run(self):
        """
        Run the plugin.
        """

        worker_metadatas = self.workflow.postbuild_results.get(PLUGIN_FETCH_WORKER_METADATA_KEY)
        comp_list = self.get_component_list_from_workers(worker_metadatas)

        if not comp_list:
            raise ValueError("No components to compare")

        package_comparison_exceptions = get_package_comparison_exceptions(self.workflow)

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
        failed = False
        for components in comp_list:
            for component in components:
                t = component['type']
                name = component['name']

                if name in package_comparison_exceptions:
                    self.log.info("Ignoring comparison of package %s", name)
                    continue

                if t not in SUPPORTED_TYPES:
                    raise ValueError("Type %s not supported" % t)

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
                        failed = True

        if failed:
            raise ValueError("Failed component comparison")
