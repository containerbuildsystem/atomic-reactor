"""
Copyright (c) 2016-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import functools
from configparser import ConfigParser
from io import StringIO

from textwrap import dedent

import re
import os

from atomic_reactor.dirs import BuildDir

from atomic_reactor.constants import (DEFAULT_DOWNLOAD_BLOCK_SIZE, PLUGIN_ADD_FILESYSTEM_KEY,
                                      PLUGIN_RESOLVE_COMPOSES_KEY)
from atomic_reactor.config import get_koji_session
from atomic_reactor.plugin import PreBuildPlugin, BuildCanceledException
from atomic_reactor.utils.koji import TaskWatcher, stream_task_output
from atomic_reactor.utils.yum import YumRepo
from atomic_reactor.util import get_platforms, base_image_is_custom, map_to_user_params
from atomic_reactor.metadata import label_map
from atomic_reactor import util
from osbs.utils import Labels


@label_map('filesystem-koji-task-id')
class AddFilesystemPlugin(PreBuildPlugin):
    """
    Creates a base image by using a filesystem generated through Koji

    Submits an image build task to Koji based on image build
    configuration file to create the filesystem to be used in
    creating the base image:
    https://docs.pagure.org/koji/image_build/

    Once image build task is complete the tarball is downloaded.
    The existing FROM instruction value is replaced with a
    FROM scratch and ADD <filesystem> <to_image> for Dockerfile
    of each platform.

    The "FROM" instruction should be in the following format:
        FROM koji/image-build[:image-build-conf]
    Where image-build-conf is the file name of the image build
    configuration to be used. If omitted, image-build.conf is used.
    This file is expected to be in the same folder as the Dockerfile.

    Runs as a pre build plugin in order to properly adjust base image.
    """

    key = PLUGIN_ADD_FILESYSTEM_KEY
    is_allowed_to_fail = False

    DEFAULT_IMAGE_BUILD_CONF = dedent('''\
        [image-build]
        name = default-name
        arches = x86_64
        format = docker
        disk_size = 10

        target = {target}

        install_tree = {install_tree}
        repo = {repo}

        ksurl = {ksurl}
        kickstart = kickstart.ks

        [factory-parameters]
        create_docker_metadata = False
        ''')

    args_from_user_params = map_to_user_params(
        "repos:yum_repourls",
        "koji_target",
    )

    def __init__(self, workflow, poll_interval=5, blocksize=DEFAULT_DOWNLOAD_BLOCK_SIZE,
                 repos=None, koji_target=None):
        """
        :param workflow: DockerBuildWorkflow instance
        :param poll_interval: int, seconds between polling Koji while waiting
                              for task completion
        :param blocksize: int, chunk size for downloading files from koji
        :param repos: list<str>: list of yum repo URLs to be used during
                      base filesystem creation. First value will also
                      be used as install_tree. Only baseurl value is used
                      from each repo file.
        :param koji_target: str, koji target name
        """
        # call parent constructor
        super(AddFilesystemPlugin, self).__init__(workflow)

        self.poll_interval = poll_interval
        self.blocksize = blocksize
        self.repos = repos or []
        self.architectures = get_platforms(self.workflow)
        self.scratch = util.is_scratch_build(self.workflow)
        self.koji_target = koji_target
        self.session = None

    def is_image_build_type(self, base_image):
        return base_image.strip().lower() == 'koji/image-build'

    def extract_base_url(self, repo_url):
        yum_repo = YumRepo(repo_url)
        yum_repo.fetch()
        if not yum_repo.is_valid():
            return []
        repo = yum_repo.config
        return [repo.get(section, 'baseurl') for section in repo.sections()
                if repo.has_option(section, 'baseurl')]

    def get_default_image_build_conf(self):
        """Create a default image build config

        :rtype: ConfigParser
        :return: Initialized config with defaults
        """
        target = self.koji_target

        vcs_info = self.workflow.source.get_vcs_info()
        ksurl = '{}#{}'.format(vcs_info.vcs_url, vcs_info.vcs_ref)

        base_urls = []
        for repo in self.repos:
            for url in self.extract_base_url(repo):
                # Imagefactory only supports $arch variable.
                url = url.replace('$basearch', '$arch')
                base_urls.append(url)

        install_tree = base_urls[0] if base_urls else ''

        repo = ','.join(base_urls)

        kwargs = {
            'target': target,
            'ksurl': ksurl,
            'install_tree': install_tree,
            'repo': repo,
        }
        config_fp = StringIO(self.DEFAULT_IMAGE_BUILD_CONF.format(**kwargs))

        config = ConfigParser()
        config.read_file(config_fp)

        self.update_config_from_dockerfile(config)

        return config

    def update_config_from_dockerfile(self, config):
        """Updates build config with values from the Dockerfile

        Updates:
          * set "name" from LABEL com.redhat.component (if exists)
          * set "version" from LABEL version (if exists)

        :param config: ConfigParser object
        """
        labels = Labels(self.workflow.build_dir.any_platform.dockerfile.labels)
        for config_key, label in (
            ('name', Labels.LABEL_TYPE_COMPONENT),
            ('version', Labels.LABEL_TYPE_VERSION),
        ):
            try:
                _, value = labels.get_name_and_value(label)
            except KeyError:
                pass
            else:
                config.set('image-build', config_key, value)

    def parse_image_build_config(self, config_file_name):

        # Logic taken from koji.cli.koji.handle_image_build.
        # Unable to re-use koji's code because "cli" is not
        # a package of koji and this logic is intermingled
        # with CLI specific instructions.

        args = []
        opts = {}

        config = self.get_default_image_build_conf()
        config.read(config_file_name)

        if self.architectures:
            config.set('image-build', 'arches', ','.join(self.architectures))
        # else just use what was provided by the user in image-build.conf

        config_str = StringIO()
        config.write(config_str)
        self.log.debug('Image Build Config: \n%s', config_str.getvalue())

        image_name = None

        section = 'image-build'
        for option in ('name', 'version', 'arches', 'target', 'install_tree'):
            value = config.get(section, option)
            if not value:
                raise ValueError('{} cannot be empty'.format(option))
            if option == 'arches':
                # pylint: disable=no-member
                value = [arch for arch in value.split(',') if arch]
            elif option == 'name':
                image_name = value
            args.append(value)
            config.remove_option(section, option)

        for option, value in config.items(section):
            if option in ('repo', 'format'):
                value = [v for v in value.split(',') if v]
            elif option in ('disk_size',):
                value = int(value)
            opts[option] = value

        section = 'ova-options'
        if config.has_section(section):
            ova = []
            for k, v in config.items(section):
                ova.append('{}={}'.format(k, v))
            opts['ova_option'] = ova

        section = 'factory-parameters'
        if config.has_section(section):
            factory = []
            for option, value in config.items(section):
                factory.append((option, value))
            opts['factory_parameter'] = factory

        return image_name, args, {'opts': opts}

    def build_filesystem(self, image_build_conf):
        # Image build conf file should be in the same folder as Dockerfile
        image_build_conf = self.workflow.build_dir.any_platform.path / image_build_conf
        if not os.path.exists(image_build_conf):
            raise RuntimeError('Image build configuration file not found: {}'
                               .format(image_build_conf))

        image_name, args, kwargs = self.parse_image_build_config(image_build_conf)

        if self.scratch:
            kwargs['opts']['scratch'] = True

        task_id = self.session.buildImageOz(*args, **kwargs)
        return task_id, image_name

    def find_filesystem(self, task_id, filesystem_regex):
        for f in self.session.listTaskOutput(task_id):
            f = f.strip()
            match = filesystem_regex.match(f)
            if match:
                return task_id, match.group(0)

        # Not found in this task, search sub tasks
        for sub_task in self.session.getTaskChildren(task_id):
            found = self.find_filesystem(sub_task['id'], filesystem_regex)
            if found:
                return found

        return None

    def download_filesystem(self, task_id, filesystem_regex, build_dir: BuildDir):
        found = self.find_filesystem(task_id, filesystem_regex)
        if found is None:
            raise RuntimeError('Filesystem not found as task output: {}'
                               .format(filesystem_regex.pattern))
        task_id, file_name = found

        self.log.info('Downloading filesystem: %s from task ID: %s',
                      file_name, task_id)

        file_path = build_dir.path / file_name
        if file_path.exists():
            raise RuntimeError(f'Filesystem {file_name} already exists at {file_path}')

        with open(file_path, 'w') as f:
            for chunk in stream_task_output(self.session, task_id, file_name, self.blocksize):
                f.write(chunk)

        return file_name

    def run_image_task(self, image_build_conf):
        task_id, image_name = self.build_filesystem(image_build_conf)

        task = TaskWatcher(self.session, task_id, self.poll_interval)
        try:
            task.wait()
        except BuildCanceledException:
            self.log.info("Build was canceled, canceling task %s", task_id)
            try:
                self.session.cancelTask(task_id)
                self.log.info('task %s canceled', task_id)
            except Exception as exc:
                self.log.info("Exception while canceling a task (ignored): %s",
                              util.exception_message(exc))

        if task.failed():
            try:
                # Koji may re-raise the error that caused task to fail
                task_result = self.session.getTaskResult(task_id)
            except Exception as exc:
                task_result = util.exception_message(exc)
            raise RuntimeError('image task, {}, failed: {}'
                               .format(task_id, task_result))

        return task_id, image_name

    def get_image_build_conf(self):
        image_build_conf = None

        for parent in self.workflow.data.dockerfile_images:
            if base_image_is_custom(parent.to_str()):
                image_build_conf = parent.tag
                break

        if not image_build_conf or image_build_conf == 'latest':
            image_build_conf = 'image-build.conf'
        return image_build_conf

    def update_repos_from_composes(self):
        resolve_comp_result = self.workflow.data.prebuild_results.get(PLUGIN_RESOLVE_COMPOSES_KEY)
        if not resolve_comp_result:
            return

        for compose_info in resolve_comp_result['composes']:
            self.log.info('adding repo file from compose: %s', compose_info['result_repofile'])
            self.repos.append(compose_info['result_repofile'])

    def _add_filesystem_to_dockerfile(self, file_name, build_dir: BuildDir):
        """
        Put an ADD instruction into the Dockerfile (to include the filesystem
        into the container image to be built)
        """
        content = 'ADD {0} /\n'.format(file_name)
        lines = build_dir.dockerfile.lines
        # as we insert elements we have to keep track of the increment for inserting
        offset = 1

        for item in build_dir.dockerfile.structure:
            if item['instruction'] == 'FROM' and base_image_is_custom(item['value'].split()[0]):
                lines.insert(item['endline']+offset, content)
                offset += 1

        build_dir.dockerfile.lines = lines
        new_parents = []

        for image in build_dir.dockerfile.parent_images:
            if base_image_is_custom(image):
                new_parents.append('scratch')
            else:
                new_parents.append(image)

        build_dir.dockerfile.parent_images = new_parents
        self.log.info('added "%s" as image filesystem', file_name)

    def inject_filesystem(self, task_id, image_name, build_dir: BuildDir):
        prefix = '{}.*{}'.format(image_name, build_dir.platform)

        pattern = (r'{}.*(\.tar|\.tar\.gz|\.tar\.bz2|\.tar\.xz)$'
                   .format(prefix))
        filesystem_regex = re.compile(pattern, re.IGNORECASE)
        file_name = self.download_filesystem(task_id, filesystem_regex, build_dir)
        self._add_filesystem_to_dockerfile(file_name, build_dir)

    def run(self):
        if not self.workflow.data.dockerfile_images.custom_parent_image:
            self.log.info('Nothing to do for non-custom base images')
            return

        self.update_repos_from_composes()

        image_build_conf = self.get_image_build_conf()

        self.session = get_koji_session(self.workflow.conf)

        task_id, image_name = self.run_image_task(image_build_conf)

        inject_filesystem_call = functools.partial(self.inject_filesystem, task_id, image_name)

        self.workflow.build_dir.for_each_platform(inject_filesystem_call)

        return {
            'filesystem-koji-task-id': task_id,
        }
