"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.plugins.build_orchestrate_build import get_worker_build_info
from atomic_reactor.constants import PLUGIN_PULP_TAG_KEY
from atomic_reactor.pulp_util import PulpHandler


class PulpTagPlugin(PostBuildPlugin):
    """
    Find a platform with a v1-image-id annotation and  and tag that docker_image in pulp as
    directed by tag_conf. Raise an error if two tags have that annotation.

    """

    key = PLUGIN_PULP_TAG_KEY
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, pulp_registry_name, pulp_secret_path=None,
                 username=None, password=None, dockpulp_loglevel=None):
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
        super(PulpTagPlugin, self).__init__(tasker, workflow)
        self.pulp_registry_name = pulp_registry_name
        self.pulp_secret_path = pulp_secret_path
        self.username = username
        self.password = password

        self.dockpulp_loglevel = dockpulp_loglevel

    def set_v1_tags(self, v1_image_id):
        image_names = self.workflow.tag_conf.images[:]
        handler = PulpHandler(self.workflow, self.pulp_registry_name, self.log,
                              pulp_secret_path=self.pulp_secret_path, username=self.username,
                              password=self.password, dockpulp_loglevel=self.dockpulp_loglevel)

        pulp_repos = handler.create_dockpulp_and_repos(image_names)
        repo_tags = {}
        for repo_id, pulp_repo in pulp_repos.items():
            repo_tags[repo_id] = {"tag": "%s:%s" % (",".join(pulp_repo.tags), v1_image_id)}
            handler.update_repo(repo_id, repo_tags[repo_id])
        return repo_tags

    def run(self):
        """
        Run the plugin.
        """

        worker_builds = self.workflow.build_result.annotations['worker-builds']
        has_v1_image_id = None
        repo_tags = {}

        for platform in worker_builds:
            build_info = get_worker_build_info(self.workflow, platform)
            annotations = build_info.build.get_annotations()
            v1_image_id = annotations.get('v1-image-id')
            if v1_image_id:
                if has_v1_image_id:
                    msg = "two platforms with v1-image-ids: {0} and {1}".format(platform,
                                                                                has_v1_image_id)
                    raise RuntimeError(msg)
                has_v1_image_id = platform
                self.log.info("tagging v1-image-id %s for platform %s", v1_image_id, platform)
                ret_val = self.set_v1_tags(v1_image_id)
                if ret_val:
                    repo_tags = ret_val
        return repo_tags
