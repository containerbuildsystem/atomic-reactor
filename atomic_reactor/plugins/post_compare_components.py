"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.constants import (PLUGIN_COMPARE_COMPONENTS_KEY,
                                      PLUGIN_FETCH_WORKER_METADATA_KEY)


SUPPORTED_TYPES = ("rpm",)


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

    def run(self):
        """
        Run the plugin.
        """

        worker_metadatas = self.workflow.postbuild_results.get(PLUGIN_FETCH_WORKER_METADATA_KEY)
        comp_list = self.get_component_list_from_workers(worker_metadatas)

        if not comp_list:
            raise ValueError("No components to compare")

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

                if t not in SUPPORTED_TYPES:
                    raise ValueError("Type %s not supported")

                identifier = (t, name)
                if identifier not in master_comp:
                    master_comp[identifier] = component
                    continue

                try:
                    mc = master_comp[identifier]

                    if t == 'rpm':
                        self.rpm_compare(mc, component)
                except ValueError as ex:
                    self.log.warn("Comparison mismatch for component %s: %s", name, ex)
                    failed = True

        if failed:
            raise ValueError("Failed component comparison")
