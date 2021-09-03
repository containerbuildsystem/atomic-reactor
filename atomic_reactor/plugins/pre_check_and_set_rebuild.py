"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import get_build_json, is_scratch_build, is_isolated_build
from atomic_reactor.config import get_openshift_session


def is_rebuild(workflow):
    return (CheckAndSetRebuildPlugin.key in workflow.prebuild_results and
            workflow.prebuild_results[CheckAndSetRebuildPlugin.key])


class CheckAndSetRebuildPlugin(PreBuildPlugin):
    """
    Determine whether this is an automated rebuild

    This plugin checks for a specific label in the OSv3 Build
    metadata. If it exists and has the value specified in the
    configuration, this build is a rebuild. The module-level function
    'is_rebuild()' can be used by other plugins to determine this.

    After checking for the label, it sets the label in the
    metadata, allowing future automated rebuilds to be detected as
    rebuilds.

    Example configuration:

    {
      "name": "check_and_set_rebuild",
      "args": {
        "label_key": "rebuild",
        "label_value": "true",
        "url": "https://localhost:8443/"
      }
    }

    """

    key = "check_and_set_rebuild"
    is_allowed_to_fail = False  # We really want to stop the process

    def __init__(self, tasker, workflow, label_key, label_value):
        """
        constructor

        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param label_key: str, key of label used to indicate first build
        :param label_value: str, value of label used to indicate first build
        """
        # call parent constructor
        super(CheckAndSetRebuildPlugin, self).__init__(tasker, workflow)
        self.label_key = label_key
        self.label_value = label_value
        self.build_labels = None

    def run(self):
        """
        run the plugin
        """
        if is_scratch_build(self.workflow):
            self.log.info('scratch build, skipping plugin')
            return False
        if is_isolated_build(self.workflow):
            self.log.info('isolated build, skipping plugin')
            return False

        if self.workflow.dockerfile_images.base_from_scratch:
            self.log.info("Skipping check and set rebuild: unsupported for FROM-scratch images")
            return False
        if self.workflow.dockerfile_images.custom_base_image:
            self.log.info("Skipping check and set rebuild: unsupported for custom base images")
            return False

        metadata = get_build_json().get("metadata", {})
        self.build_labels = metadata.get("labels", {})
        buildconfig = self.build_labels["buildconfig"]
        is_rebuild = self.build_labels.get(self.label_key) == self.label_value
        self.log.info("This is a rebuild? %s", is_rebuild)

        if not is_rebuild:
            # Update the BuildConfig metadata so the next Build
            # instantiated from it is detected as being an automated
            # rebuild
            osbs = get_openshift_session(self.workflow.conf,
                                         self.workflow.user_params.get('namespace'))
            new_labels = {self.label_key: self.label_value}
            osbs.update_labels_on_build_config(buildconfig, new_labels)
        else:
            self.pull_latest_commit_if_configured()

        return is_rebuild

    def pull_latest_commit_if_configured(self):
        if not self.should_use_latest_commit():
            return

        git_branch = self.build_labels['git-branch']
        self.workflow.source.reset('origin/{}'.format(git_branch))

        # Import it here to avoid circular import errors.
        from atomic_reactor.plugins.build_orchestrate_build import override_build_kwarg
        override_build_kwarg(self.workflow, 'git_ref', self.workflow.source.commit_id)

    def should_use_latest_commit(self):
        return self.workflow.source.config.autorebuild.get('from_latest', False)
