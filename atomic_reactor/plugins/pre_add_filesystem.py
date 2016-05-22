"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import print_function, unicode_literals

try:
    # py2
    from ConfigParser import ConfigParser
except ImportError:
    # py3
    from configparser import ConfigParser

import json
import re
import os

from atomic_reactor.constants import DEFAULT_DOWNLOAD_BLOCK_SIZE
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.koji_util import create_koji_session, TaskWatcher, stream_task_output


class AddFilesystemPlugin(PreBuildPlugin):
    """
    Creates a base image by using a filesystem generated through Koji

    Submits an image build task to Koji based on image build
    configuration file to create the filesystem to be used in
    creating the base image:
    https://fedoraproject.org/wiki/Koji/BuildingImages#Building_Disk_Images

    Once image build task is complete the tarball is downloaded and
    it's imported into docker. This creates a new image. The existing
    FROM instruction value is replaced with the ID of this new image.

    The "FROM" instruction should be in the following format:
        FROM koji/image-build[:image-build-conf]
    Where image-build-conf is the file name of the image build
    configuration to be used. If omitted, image-build.conf is used.
    This file is expected to be in the same folder as the Dockerfile.

    Runs as a pre build plugin in order to properly adjust base image.
    """

    key = 'add_filesystem'
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, koji_hub,
                 koji_proxyuser=None, koji_ssl_certs_dir=None,
                 koji_krb_principal=None, koji_krb_keytab=None,
                 from_task_id=None, poll_interval=5,
                 blocksize=DEFAULT_DOWNLOAD_BLOCK_SIZE):
        """
        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param koji_hub: str, koji hub (xmlrpc)
        :param koji_proxyuser: str, proxy user
        :param koji_ssl_certs_dir: str, path to "cert", "ca", and "serverca"
        :param koji_krb_principal: str, name of Kerberos principal
        :param koji_krb_keytab: str, Kerberos keytab
        :param from_task_id: int, use existing Koji image task ID
        :param poll_interval: int, seconds between polling Koji while waiting
                              for task completion
        :param blocksize: int, chunk size for streaming files from koji
        """
        # call parent constructor
        super(AddFilesystemPlugin, self).__init__(tasker, workflow)
        self.koji_hub = koji_hub
        self.koji_auth_info = {
            'proxyuser': koji_proxyuser,
            'ssl_certs_dir': koji_ssl_certs_dir,
            'krb_principal': koji_krb_principal,
            'krb_keytab': koji_krb_keytab,
        }
        self.from_task_id = from_task_id
        self.poll_interval = poll_interval
        self.blocksize = blocksize

    def is_image_build_type(self, base_image):
        return base_image.strip().lower() == 'koji/image-build'

    def parse_image_build_config(self, config_file_name):

        # Logic taken from koji.cli.koji.handle_image_build.
        # Unable to re-use koji's code because "cli" is not
        # a package of koji and this logic is intermingled
        # with CLI specific instructions.

        args = []
        opts = {}

        config = ConfigParser()
        config.read(config_file_name)

        image_name = None

        section = 'image-build'
        for option in ('name', 'version', 'arches', 'target', 'install_tree'):
            value = config.get(section, option)
            if option == 'arches':
                value = [arch for arch in value.split(',') if arch]
            elif option == 'name':
                image_name = value
            args.append(value)
            config.remove_option(section, option)

        for option, value in config.items(section):
            if option in ('repo', 'format'):
                value = [v for v in value.split(',') if v]
            elif option in ('disk_size'):
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

        # Set some defaults.
        opts.setdefault('disk_size', 10)

        return image_name, args, {'opts': opts}

    def build_filesystem(self, image_build_conf):
        # Image build conf file should be in the same folder as Dockerfile
        df_path, df_dir = self.workflow.source.get_dockerfile_path()
        image_build_conf = os.path.join(df_dir, image_build_conf)
        if not os.path.exists(image_build_conf):
            raise RuntimeError('Image build configuration file not found: {}'
                               .format(image_build_conf))

        image_name, args, kwargs = self.parse_image_build_config(image_build_conf)
        pattern = ('{}.*(\.tar|\.tar\.gz|\.tar\.bz2|\.tar\.xz)$'
                   .format(image_name))
        filesystem_regex = re.compile(pattern, re.IGNORECASE)
        if self.from_task_id:
            task_id = self.from_task_id
        else:
            task_id = self.session.buildImageOz(*args, **kwargs)
        return task_id, filesystem_regex

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

    def download_filesystem(self, task_id, filesystem_regex):
        found = self.find_filesystem(task_id, filesystem_regex)
        if found is None:
            raise RuntimeError('Filesystem not found as task output: {}'
                               .format(filesystem_regex.pattern))
        task_id, file_name = found

        self.log.info('Streaming filesystem: %s from task ID: %s',
                      file_name, task_id)

        contents = stream_task_output(self.session, task_id, file_name,
                                      self.blocksize)

        return contents

    def import_base_image(self, filesystem):
        result = self.tasker.d.import_image_from_stream(filesystem)
        # Response not deserialized:
        #   https://github.com/docker/docker-py/issues/1060
        self.log.info('import base image result: %s', result)
        result = json.loads(result)
        return result['status']

    def run(self):
        base_image = self.workflow.builder.base_image
        if base_image.namespace != 'koji' or base_image.repo != 'image-build':
            self.log.info('Base image not supported: %s', base_image)
            return

        image_build_conf = base_image.tag
        if not image_build_conf or image_build_conf == 'latest':
            image_build_conf = 'image-build.conf'

        self.session = create_koji_session(self.koji_hub, self.koji_auth_info)

        task_id, filesystem_regex = self.build_filesystem(image_build_conf)

        task = TaskWatcher(self.session, task_id, self.poll_interval)
        task.wait()
        if task.failed():
            raise RuntimeError('Create filesystem task failed: {}'
                               .format(task_id))

        filesystem = self.download_filesystem(task_id, filesystem_regex)

        new_base_image = self.import_base_image(filesystem)
        self.workflow.builder.set_base_image(new_base_image)

        return {
            'base-image-id': new_base_image,
            'filesystem-koji-task-id': task_id,
        }
