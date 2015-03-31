"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from dock.plugin import PostBuildPlugin


__all__ = ('PostBuildRPMqaPlugin', )


class PostBuildRPMqaPlugin(PostBuildPlugin):
    key = "all_rpm_packages"

    def __init__(self, tasker, workflow, image_id):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        """
        # call parent constructor
        super(PostBuildRPMqaPlugin, self).__init__(tasker, workflow)
        self.image_id = image_id

    def run(self):
        container_id = self.tasker.run(
            self.image_id,
            command='-qa',
            create_kwargs={"entrypoint": "/bin/rpm"},
            start_kwargs={},
        )
        self.tasker.wait(container_id)
        plugin_output = self.tasker.logs(container_id, stream=False)
        self.tasker.remove_container(container_id)
        return plugin_output
