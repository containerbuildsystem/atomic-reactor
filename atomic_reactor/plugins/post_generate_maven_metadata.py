"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import koji

from atomic_reactor import util
from atomic_reactor.constants import PLUGIN_GENERATE_MAVEN_METADATA_KEY
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.plugins.pre_reactor_config import get_koji_session
from atomic_reactor.utils.koji import NvrRequest


class GenerateMavenMetadataPlugin(PostBuildPlugin):
    """
    Generate maven metadata
    """

    key = PLUGIN_GENERATE_MAVEN_METADATA_KEY
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow):
        """
        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        """
        super(GenerateMavenMetadataPlugin, self).__init__(tasker, workflow)
        self.session = None

    def get_nvr_components(self, nvr_requests):
        components = []

        for nvr_request in nvr_requests:
            # We're assuming here that we won't run into any errors here
            #  since this plugin runs after pre_fetch_maven_artifacts
            #  that should fail if there were any errors.
            build_info = self.session.getBuild(nvr_request.nvr)
            build_archives = self.session.listArchives(buildID=build_info['id'],
                                                       type='maven')
            build_archives = nvr_request.match_all(build_archives)

            for build_archive in build_archives:
                checksum_type = koji.CHECKSUM_TYPES[build_archive['checksum_type']]
                components.append({
                    'type': 'kojifile',
                    'filename': build_archive['filename'],
                    'filesize': build_archive['size'],
                    'checksum': build_archive['checksum'],
                    'checksum_type': checksum_type,
                    'nvr': nvr_request.nvr,
                    'archive_id': build_archive['id'],
                })

        return components

    def run(self):
        """
        Run the plugin.
        """

        self.session = get_koji_session(self.workflow)

        nvr_requests = [
            NvrRequest(**nvr_request) for nvr_request in
            util.read_fetch_artifacts_koji(self.workflow) or []
        ]

        components = self.get_nvr_components(nvr_requests)

        return {'components': components}
