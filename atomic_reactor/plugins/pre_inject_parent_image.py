"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import print_function, unicode_literals

from atomic_reactor.build import ImageName
from atomic_reactor.koji_util import create_koji_session
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.exit_remove_built_image import defer_removal
from osbs.utils import graceful_chain_get


class InjectParentImage(PreBuildPlugin):
    """
    Modifies parent image to be used based on given Koji build.

    It first attempts to find the list of available repositories
    from '.extra.image.index.pull' in Koji build information. If
    not found, the first archive in Koji build that defines a non-empty
    '.extra.docker.repositories' list is used.

    This list provides the pull reference for the container image
    associated with Koji build. If it contains multiple item, the
    manifest digest, @sha256, is preferred. Otherwise, the first
    repository in list is used.

    The namespace and repository for the new parent image must match
    the namespace and repository for the parent image defined in
    Dockerfile.

    This plugin returns the identifier of the Koji build used.
    """

    key = 'inject_parent_image'
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, koji_parent_build, koji_hub, koji_ssl_certs_dir=None):
        """
        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param koji_parent_build: str, either Koji build ID or Koji build NVR
        :param koji_hub: str, koji hub (xmlrpc)
        :param koji_ssl_certs_dir: str, path to "cert", "ca", and "serverca"
                                   used when Koji's identity certificate is not trusted
        """
        super(InjectParentImage, self).__init__(tasker, workflow)

        koji_auth_info = None
        if koji_ssl_certs_dir:
            koji_auth_info = {
                'ssl_certs_dir': koji_ssl_certs_dir,
            }
        self.koji_session = create_koji_session(koji_hub, koji_auth_info)

        try:
            self.koji_parent_build = int(koji_parent_build)
        except ValueError:
            self.koji_parent_build = koji_parent_build

        self._koji_parent_build_info = None
        self._repositories = None
        self._new_parent_image = None

    def run(self):
        self.find_repositories()
        self.select_new_parent_image()
        self.validate_new_parent_image()
        self.set_new_parent_image()
        return self._koji_parent_build_info['id']

    def find_repositories(self):
        self._repositories = (self.find_repositories_from_build() or
                              self.find_repositories_from_archive())

        if not self._repositories:
            raise RuntimeError('A suitable archive for Koji build {[nvr]} was not found'
                               .format(self._koji_parent_build_info))

    def find_repositories_from_build(self):
        self._koji_parent_build_info = self.koji_session.getBuild(self.koji_parent_build)
        if not self._koji_parent_build_info:
            raise RuntimeError('Koji build, {}, not found'.format(self.koji_parent_build))

        repositories = graceful_chain_get(self._koji_parent_build_info,
                                          'extra', 'image', 'index', 'pull')
        if repositories:
            self.log.info('Using repositories from build info')

        return repositories

    def find_repositories_from_archive(self):
        for archive in self.koji_session.listArchives(self._koji_parent_build_info['id']):
            repositories = graceful_chain_get(archive, 'extra', 'docker', 'repositories')
            if repositories:
                self.log.info('Using repositories from archive %d', archive['id'])
                return repositories

        return None

    def select_new_parent_image(self):
        for repository in self._repositories:
            if '@' in repository:
                self._new_parent_image = repository
                break

        # v2 manifest digest, not found, just pick the first one.
        if not self._new_parent_image:
            self._new_parent_image = self._repositories[0]

        self.log.info('New parent image is %s', self._new_parent_image)

    def validate_new_parent_image(self):
        current_repo = self.workflow.builder.base_image.to_str(registry=False, tag=False)
        new_repo = ImageName.parse(self._new_parent_image).to_str(registry=False, tag=False)

        if current_repo != new_repo:
            raise RuntimeError(
                'Repository for new parent image, {}, differs from '
                'repository for existing parent image, {}'.format(new_repo, current_repo))

    def set_new_parent_image(self):
        self.workflow.builder.set_base_image(self._new_parent_image)
        defer_removal(self.workflow, self._new_parent_image)
