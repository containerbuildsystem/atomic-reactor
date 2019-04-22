"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import absolute_import

import os

from six.moves.urllib.parse import urlsplit, urlunsplit
from six.moves.configparser import ConfigParser

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_reactor_config import get_yum_proxies
from atomic_reactor.util import df_parser


class YumProxyPlugin(PreBuildPlugin):
    key = 'yum_proxy'
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow):
        """
        Plugin initializer

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        """
        super(YumProxyPlugin, self).__init__(tasker, workflow)

        # TODO: Is this dir accessbile in the build context?
        self.workdir = os.path.join(self.workflow.source.workdir, 'atomic-reactor-yum-repos')
        self.all_yum_repo_files = {}
        self.updated_yum_repo_files = {}
        self.yum_proxies = None
        # TODO: Clean up containers and its volumes.
        self._container_ids = []

    def run(self):
        """
        run the plugin
        """
        try:
            yum_proxies = get_yum_proxies(self.workflow)
        except KeyError:
            return
        # Let's make it easier to search which hosts should be proxied.
        self.yum_proxies = {info['proxied']: info['proxy'] for info in yum_proxies}

        # Iterate through each parent image to find any hard coded yum repo file.
        for parent_image_id in self.workflow.builder.parent_images.keys():
            # TODO: will this ^ ignore SCRATCH_FROM ? Needs testing, but it looks like
            # SCRATCH_FROM are exluded from parent_images .
            self.all_yum_repo_files[parent_image_id] = self._get_yum_repos(parent_image_id)

        self._generate_new_yum_repo_files()
        self._update_dockerfile()

    def _get_yum_repos(self, image_id):
        # TODO: This needs a retry
        dest_path = os.path.join(self.workdir, 'original', image_id)
        try:
            os.makedirs(dest_path)
        except OSError:
            # Ignore if dirs have already been created. Python 2 doesn't suport exists_ok param
            pass
        container_id = self.tasker.copy_file(
            image_id,
            src_path='/etc/yum.repos.d',
            dest_path=dest_path,
            create_kwargs={'user': 'root'},
        )
        self._container_ids.append(container_id)
        return self._parse_yum_repos(dest_path)

    def _parse_yum_repos(self, dest_path):
        dest_path = os.path.join(dest_path, 'yum.repos.d')
        yum_repo_files = {}
        for repo_file_path in os.listdir(dest_path):
            repo_file_content = ConfigParser()
            repo_file_content.read(os.path.join(dest_path, repo_file_path))
            yum_repo_files[repo_file_path] = repo_file_content
        return yum_repo_files

    def _generate_new_yum_repo_files(self):
        for parent_image_id, yum_repo_files in self.all_yum_repo_files.items():
            for yum_repo_file_name, yum_repo_file_content in yum_repo_files.items():
                rewrite_file = False
                for yum_repo_name in yum_repo_file_content.sections():
                    baseurl = yum_repo_file_content.get(yum_repo_name, 'baseurl')
                    if not baseurl:
                        continue
                    parts = urlsplit(baseurl)
                    if parts.netloc in self.yum_proxies:
                        # TODO: Do we also need to replace URL schema?
                        proxied_baseurl = urlunsplit(
                            (parts[0], self.yum_proxies[parts.netloc]) + parts[2:])
                        yum_repo_file_content.set(yum_repo_name, 'baseurl', proxied_baseurl)
                        rewrite_file = True

                if rewrite_file:
                    dest_path = os.path.join(self.workdir, 'updated', parent_image_id,
                                             yum_repo_file_name)
                    self._write_yum_repo_file(dest_path, yum_repo_file_content)
                    self.updated_yum_repo_files.setdefault(parent_image_id, [])
                    self.updated_yum_repo_files[parent_image_id].append(yum_repo_file_name)

    def _write_yum_repo_file(self, dest_path, content):
        try:
            os.makedirs(os.path.dirname(dest_path))
        except OSError:
            # Ignore if dirs have already been created. Python 2 doesn't suport exists_ok param
            pass
        with open(dest_path, 'w') as f:
            for yum_repo_name in content.sections():
                f.write('[{}]\n'.format(yum_repo_name))
                for key, value in content.items(yum_repo_name):
                    f.write('{} = {}\n'.format(key, value))

    def _update_dockerfile(self):
        dfp = df_parser(self.workflow.builder.df_path)

        for parent_image_id, yum_repo_files in self.updated_yum_repo_files.items():
            if not yum_repo_files:
                continue

            parent_structure = self._find_parent_structure(dfp, parent_image_id)
            update_lines = []
            for yum_repo_file_name in sorted(yum_repo_files):
                update_path = os.path.join(self.workdir, 'updated', parent_image_id,
                                           yum_repo_file_name)
                update_lines.append('ADD {} /etc/yum.repos.d/'.format(update_path))
            dfp.add_lines_at(parent_structure, *update_lines, after=True)

            last_stage_structure = self._find_last_stage_structure(dfp, parent_structure)
            original_lines = []
            for yum_repo_file_name in sorted(yum_repo_files):
                original_path = os.path.join(self.workdir, 'original', parent_image_id,
                                             yum_repo_file_name)
                original_lines.append('ADD {} /etc/yum.repos.d/'.format(original_path))
            dfp.add_lines_at(last_stage_structure, *original_lines, after=True)

    def _find_parent_structure(self, dfp, parent_image_id):
        for structure in dfp.structure:
            if structure['instruction'] != 'FROM':
                continue
            found_parent_image_id = structure['value'].split(' ')[0]
            if found_parent_image_id == parent_image_id:
                return structure

        raise RuntimeError('Unable to find parent image instruction')

    def _find_last_stage_structure(self, dfp, parent_structure):
        partial_structure = dfp.structure[dfp.structure.index(parent_structure)+1:]
        last_structure = parent_structure
        for structure in partial_structure:
            if structure['instruction'] == 'FROM':
                break
            last_structure = structure
        return last_structure
