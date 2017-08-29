"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Push built image to crane or delete them if the build failed

Several authentication schemes are possible, including
username+password and key/certificate via secrets.

However, the recommended scheme (since Origin 1.0.6) is to store a
key and certificate in a secret which the builder service account is
allowed to mount:

$ oc secrets new pulp ./pulp.key ./pulp.cer
secrets/pulp
$ oc secrets add serviceaccount/builder secret/pulp --for=mount

In the BuildConfig for atomic-reactor, specify the secret in the
strategy's 'secrets' array, specifying a mount path:

"secrets": [{
  "secretSource": {
    "name": "pulp"
  },
  "mountPath": "/var/run/secrets/pulp"
}]

In the configuration for this plugin, specify the same path for
pulp_secret_path:

"pulp_publish": {
  "pulp_registry_name": ...,
  ...
  "pulp_secret_path": "/var/run/secrets/pulp"
}
"""

from __future__ import print_function, unicode_literals

from atomic_reactor.constants import PLUGIN_PULP_PUBLISH_KEY
from atomic_reactor.plugins.build_orchestrate_build import get_worker_build_info
from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.util import ImageName
from atomic_reactor.pulp_util import PulpHandler


class PulpPublishPlugin(ExitPlugin):
    key = PLUGIN_PULP_PUBLISH_KEY
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, pulp_registry_name,
                 pulp_secret_path=None, username=None, password=None,
                 dockpulp_loglevel=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param pulp_registry_name: str, name of pulp registry to use, specified in /etc/
                                   dockpulp.conf
        :param pulp_secret_path: path to pulp.cer and pulp.key; $SOURCE_SECRET_PATH otherwise
        :param username: pulp username, used in preference to certificate and key
        :param password: pulp password, used in preference to certificate and key
        """
        # call parent constructor
        super(PulpPublishPlugin, self).__init__(tasker, workflow)
        self.workflow = workflow
        self.pulp_handler = PulpHandler(self.workflow, pulp_registry_name, self.log,
                                        pulp_secret_path=pulp_secret_path,
                                        username=username, password=password,
                                        dockpulp_loglevel=dockpulp_loglevel)

    def publish_to_crane(self, repo_prefix="redhat-"):
        image_names = self.workflow.tag_conf.images[:]
        # Find out how to publish this image.
        self.log.info("image names: %s", [str(image_name) for image_name in image_names])

        self.pulp_handler.create_dockpulp()
        if not repo_prefix:
            repo_prefix = ''
        pulp_repos = set(['%s%s' % (repo_prefix, image.pulp_repo)
                          for image in image_names])
        self.pulp_handler.publish(pulp_repos)

        pulp_registry = self.pulp_handler.get_registry_hostname()
        crane_repos = [ImageName(registry=pulp_registry,
                                 repo=image.to_str(registry=False, tag=False),
                                 tag=image.tag or 'latest') for image in image_names]

        for image_name in crane_repos:
            self.log.info("image available at %s", str(image_name))

        return crane_repos

    def delete_v1_layers(self, repo_prefix="redhat-"):
        annotations = self.workflow.build_result.annotations
        if not annotations:
            # No worker builds created
            return

        worker_builds = annotations['worker-builds']

        for platform in worker_builds:
            build_info = get_worker_build_info(self.workflow, platform)
            annotations = build_info.build.get_annotations()
            v1_image_id = annotations.get('v1-image-id')
            if v1_image_id:
                image_names = self.workflow.tag_conf.images
                self.pulp_handler.create_dockpulp()
                if not repo_prefix:
                    repo_prefix = ''
                pulp_repos = set(['%s%s' % (repo_prefix, image.pulp_repo) for image in image_names])
                for repo_id in pulp_repos:
                    self.log.info("removing %s from repo %s", v1_image_id, repo_id)
                    self.pulp_handler.remove_image(repo_id, v1_image_id)

    def run(self):
        if self.workflow.build_process_failed:
            self.delete_v1_layers()
            return []
        else:
            return self.publish_to_crane()
