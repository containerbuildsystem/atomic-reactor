"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from collections import namedtuple
import json
import os
import random
from string import ascii_letters
import subprocess
from tempfile import NamedTemporaryFile
import time

from atomic_reactor import __version__ as atomic_reactor_version
from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.source import GitSource
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin
from atomic_reactor.plugins.pre_add_filesystem import AddFilesystemPlugin
from atomic_reactor.constants import PROG
from atomic_reactor.util import (get_version_of_tools, get_checksums,
                                 get_build_json, get_preferred_label)
from atomic_reactor.koji_util import create_koji_session, TaskWatcher
from dockerfile_parse import DockerfileParser
from osbs.conf import Configuration
from osbs.api import OSBS
from osbs.exceptions import OsbsException

# An output file and its metadata
Output = namedtuple('Output', ['file', 'metadata'])


class KojiUploadLogger(object):
    def __init__(self, logger, notable_percent=10):
        self.logger = logger
        self.notable_percent = notable_percent
        self.last_percent_done = 0

    def callback(self, offset, totalsize, size, t1, t2): # pylint: disable=W0613
        if offset == 0:
            self.logger.debug("upload size: %.1fMiB", totalsize / 1024.0 / 1024)

        if not totalsize or not t1:
            return

        percent_done = 100 * offset / totalsize
        if (percent_done >= 99 or
                percent_done - self.last_percent_done >= self.notable_percent):
            self.last_percent_done = percent_done
            self.logger.debug("upload: %d%% done (%.1f MiB/sec)",
                              percent_done, size / t1 / 1024 / 1024)


class KojiPromotePlugin(ExitPlugin):
    """
    Promote this build to Koji

    Submits a successful build to Koji using the Content Generator API,
    https://fedoraproject.org/wiki/Koji/ContentGenerators

    Authentication is with Kerberos unless the koji_ssl_certs
    configuration parameter is given, in which case it should be a
    path at which 'cert', 'ca', and 'serverca' are the certificates
    for SSL authentication.

    If Kerberos is used for authentication, the default principal will
    be used (from the kernel keyring) unless both koji_keytab and
    koji_principal are specified. The koji_keytab parameter is a
    keytab name like 'type:name', and so can be used to specify a key
    in a Kubernetes secret by specifying 'FILE:/path/to/key'.

    If metadata_only is set, the 'docker save' image will not be
    uploaded, only the logs. The import will be marked as
    metadata-only.

    Runs as an exit plugin in order to capture logs from all other
    plugins.
    """

    key = "koji_promote"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, kojihub, url,
                 verify_ssl=True, use_auth=True,
                 koji_ssl_certs=None, koji_proxy_user=None,
                 koji_principal=None, koji_keytab=None,
                 metadata_only=False, blocksize=None,
                 target=None, poll_interval=5):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param kojihub: string, koji hub (xmlrpc)
        :param url: string, URL for OSv3 instance
        :param verify_ssl: bool, verify OSv3 SSL certificate?
        :param use_auth: bool, initiate authentication with OSv3?
        :param koji_ssl_certs: str, path to 'cert', 'ca', 'serverca'
        :param koji_proxy_user: str, user to log in as (requires hub config)
        :param koji_principal: str, Kerberos principal (must specify keytab)
        :param koji_keytab: str, keytab name (must specify principal)
        :param metadata_only: bool, whether to omit the 'docker save' image
        :param blocksize: int, blocksize to use for uploading files
        :param target: str, koji target
        :param poll_interval: int, seconds between Koji task status requests
        """
        super(KojiPromotePlugin, self).__init__(tasker, workflow)

        self.kojihub = kojihub
        self.koji_ssl_certs = koji_ssl_certs
        self.koji_proxy_user = koji_proxy_user
        self.koji_principal = koji_principal
        self.koji_keytab = koji_keytab
        self.metadata_only = metadata_only
        self.blocksize = blocksize
        self.target = target
        self.poll_interval = poll_interval

        self.namespace = get_build_json().get('metadata', {}).get('namespace', None)
        osbs_conf = Configuration(conf_file=None, openshift_uri=url,
                                  use_auth=use_auth, verify_ssl=verify_ssl,
                                  namespace=self.namespace)
        self.osbs = OSBS(osbs_conf, osbs_conf)
        self.build_id = None
        self.nvr_image = None

    @staticmethod
    def parse_rpm_output(output, tags, separator=';'):
        """
        Parse output of the rpm query.

        :param output: list, decoded output (str) from the rpm subprocess
        :param tags: list, str fields used for query output
        :return: list, dicts describing each rpm package
        """

        def field(tag):
            """
            Get a field value by name
            """
            try:
                value = fields[tags.index(tag)]
            except ValueError:
                return None

            if value == '(none)':
                return None

            return value

        components = []
        sigmarker = 'Key ID '
        for rpm in output:
            fields = rpm.rstrip('\n').split(separator)
            if len(fields) < len(tags):
                continue

            signature = field('SIGPGP:pgpsig') or field('SIGGPG:pgpsig')
            if signature:
                parts = signature.split(sigmarker, 1)
                if len(parts) > 1:
                    signature = parts[1]

            component_rpm = {
                'type': 'rpm',
                'name': field('NAME'),
                'version': field('VERSION'),
                'release': field('RELEASE'),
                'arch': field('ARCH'),
                'sigmd5': field('SIGMD5'),
                'signature': signature,
            }

            # Special handling for epoch as it must be an integer or None
            epoch = field('EPOCH')
            if epoch is not None:
                epoch = int(epoch)

            component_rpm['epoch'] = epoch

            if component_rpm['name'] != 'gpg-pubkey':
                components.append(component_rpm)

        return components

    def get_rpms(self):
        """
        Build a list of installed RPMs in the format required for the
        metadata.
        """

        tags = [
            'NAME',
            'VERSION',
            'RELEASE',
            'ARCH',
            'EPOCH',
            'SIGMD5',
            'SIGPGP:pgpsig',
            'SIGGPG:pgpsig',
        ]

        sep = ';'
        fmt = sep.join(["%%{%s}" % tag for tag in tags])
        cmd = "/bin/rpm -qa --qf '{0}\n'".format(fmt)
        try:
            # py3
            (status, output) = subprocess.getstatusoutput(cmd)
        except AttributeError:
            # py2
            with open('/dev/null', 'r+') as devnull:
                p = subprocess.Popen(cmd,
                                     shell=True,
                                     stdin=devnull,
                                     stdout=subprocess.PIPE,
                                     stderr=devnull)

                (stdout, stderr) = p.communicate()
                status = p.wait()
                output = stdout.decode()

        if status != 0:
            self.log.debug("%s: stderr output: %s", cmd, stderr)
            raise RuntimeError("%s: exit code %s" % (cmd, status))

        return self.parse_rpm_output(output.splitlines(), tags, separator=sep)

    def get_output_metadata(self, path, filename):
        """
        Describe a file by its metadata.

        :return: dict
        """

        checksums = get_checksums(path, ['md5'])
        metadata = {'filename': filename,
                    'filesize': os.path.getsize(path),
                    'checksum': checksums['md5sum'],
                    'checksum_type': 'md5'}

        if self.metadata_only:
            metadata['metadata_only'] = True

        return metadata

    def get_builder_image_id(self):
        """
        Find out the docker ID of the buildroot image we are in.
        """

        try:
            buildroot_tag = os.environ["OPENSHIFT_CUSTOM_BUILD_BASE_IMAGE"]
        except KeyError:
            return ''

        try:
            pod = self.osbs.get_pod_for_build(self.build_id)
            all_images = pod.get_container_image_ids()
        except OsbsException as ex:
            self.log.error("unable to find image id: %r", ex)
            return buildroot_tag

        try:
            return all_images[buildroot_tag]
        except KeyError:
            self.log.error("Unable to determine buildroot image ID for %s",
                           buildroot_tag)
            return buildroot_tag

    def get_buildroot(self, build_id):
        """
        Build the buildroot entry of the metadata.

        :return: dict, partial metadata
        """

        docker_version = self.tasker.get_version()
        docker_info = self.tasker.get_info()
        host_arch = docker_version['Arch']
        if host_arch == 'amd64':
            host_arch = 'x86_64'

        buildroot = {
            'id': 1,
            'host': {
                'os': docker_info['OperatingSystem'],
                'arch': host_arch,
            },
            'content_generator': {
                'name': PROG,
                'version': atomic_reactor_version,
            },
            'container': {
                'type': 'docker',
                'arch': os.uname()[4],
            },
            'tools': [
                {
                    'name': tool['name'],
                    'version': tool['version'],
                }
                for tool in get_version_of_tools()] + [
                {
                    'name': 'docker',
                    'version': docker_version['Version'],
                },
            ],
            'components': self.get_rpms(),
            'extra': {
                'osbs': {
                    'build_id': build_id,
                    'builder_image_id': self.get_builder_image_id(),
                }
            },
        }

        return buildroot

    def get_logs(self):
        """
        Build the logs entry for the metadata 'output' section

        :return: list, Output instances
        """

        output = []

        # Collect logs from server
        try:
            logs = self.osbs.get_build_logs(self.build_id)
        except OsbsException as ex:
            self.log.error("unable to get build logs: %r", ex)
        else:
            # Deleted once closed
            logfile = NamedTemporaryFile(prefix=self.build_id,
                                         suffix=".log",
                                         mode='w')
            logfile.write(logs)
            logfile.flush()
            metadata = self.get_output_metadata(logfile.name,
                                                "openshift-final.log")
            output.append(Output(file=logfile, metadata=metadata))

        docker_logs = NamedTemporaryFile(prefix="docker-%s" % self.build_id,
                                         suffix=".log",
                                         mode='w')
        docker_logs.write("\n".join(self.workflow.build_logs))
        docker_logs.flush()
        output.append(Output(file=docker_logs,
                             metadata=self.get_output_metadata(docker_logs.name,
                                                               "build.log")))
        return output

    def get_image_components(self):
        """
        Re-package the output of the rpmqa plugin into the format required
        for the metadata.
        """

        try:
            output = self.workflow.postbuild_results[PostBuildRPMqaPlugin.key]
        except KeyError:
            self.log.error("%s plugin did not run!",
                           PostBuildRPMqaPlugin.key)
            return []

        return self.parse_rpm_output(output, PostBuildRPMqaPlugin.rpm_tags,
                                     separator=',')

    def get_image_output(self, arch):
        """
        Create the output for the image

        This is the Koji Content Generator metadata, along with the
        'docker save' output to upload.

        For metadata-only builds, an empty file is used instead of the
        output of 'docker save'.

        :param arch: str, architecture for this output
        :return: tuple, (metadata dict, Output instance)

        """

        image_id = self.workflow.builder.image_id
        saved_image = self.workflow.exported_image_sequence[-1].get('path')
        ext = saved_image.split('.', 1)[1]
        name_fmt = 'docker-image-{id}.{arch}.{ext}'
        image_name = name_fmt.format(id=image_id, arch=arch, ext=ext)
        if self.metadata_only:
            metadata = self.get_output_metadata(os.path.devnull, image_name)
            output = Output(file=None, metadata=metadata)
        else:
            metadata = self.get_output_metadata(saved_image, image_name)
            output = Output(file=open(saved_image), metadata=metadata)

        return metadata, output

    def get_digests(self):
        """
        Returns a map of repositories to digests
        """

        digests = {}  # repository -> digest
        for registry in self.workflow.push_conf.docker_registries:
            for image in self.workflow.tag_conf.images:
                image_str = image.to_str()
                if image_str in registry.digests:
                    digest = registry.digests[image_str]
                    digests[image.to_str(registry=False)] = digest

        return digests

    def get_repositories(self, digests):
        """
        Build the repositories metadata

        :param digests: dict, repository -> digest
        """
        if self.workflow.push_conf.pulp_registries:
            # If pulp was used, only report pulp images
            registries = self.workflow.push_conf.pulp_registries
        else:
            # Otherwise report all the images we pushed
            registries = self.workflow.push_conf.all_registries

        output_images = []
        for registry in registries:
            image = self.nvr_image.copy()
            image.registry = registry.uri
            pullspec = image.to_str()

            output_images.append(pullspec)

            digest = digests.get(image.to_str(registry=False))
            if digest:
                digest_pullspec = image.to_str(tag=False) + "@" + digest
                output_images.append(digest_pullspec)

        return output_images

    def get_output(self, buildroot_id):
        """
        Build the 'output' section of the metadata.

        :return: list, Output instances
        """

        def add_buildroot_id(output):
            logfile, metadata = output
            metadata.update({'buildroot_id': buildroot_id})
            return Output(file=logfile, metadata=metadata)

        def add_log_type(output):
            logfile, metadata = output
            metadata.update({'type': 'log', 'arch': 'noarch'})
            return Output(file=logfile, metadata=metadata)

        output_files = [add_log_type(add_buildroot_id(metadata))
                        for metadata in self.get_logs()]

        # Parent of squashed built image is base image
        image_id = self.workflow.builder.image_id
        parent_id = self.workflow.base_image_inspect['Id']
        digests = self.get_digests()
        repositories = self.get_repositories(digests)
        arch = os.uname()[4]
        metadata, output = self.get_image_output(arch)
        metadata.update({
            'arch': arch,
            'type': 'docker-image',
            'components': self.get_image_components(),
            'extra': {
                'image': {
                    'arch': arch,
                },
                'docker': {
                    'id': image_id,
                    'parent_id': parent_id,
                    'repositories': repositories,
                },
            },
        })

        # Add the 'docker save' image to the output
        image = add_buildroot_id(output)
        output_files.append(image)

        return output_files

    def get_build(self, metadata):
        build_start_time = metadata["creationTimestamp"]
        try:
            # Decode UTC RFC3339 date with no fractional seconds
            # (the format we expect)
            start_time_struct = time.strptime(build_start_time,
                                              '%Y-%m-%dT%H:%M:%SZ')
            start_time = int(time.mktime(start_time_struct))
        except ValueError:
            self.log.error("Invalid time format (%s)", build_start_time)
            raise

        labels = DockerfileParser(self.workflow.builder.df_path).labels
        component = get_preferred_label(labels, 'com.redhat.component')
        version = get_preferred_label(labels, 'version')
        release = get_preferred_label(labels, 'release')

        source = self.workflow.source
        if not isinstance(source, GitSource):
            raise RuntimeError('git source required')

        extra = {'image': {}}
        koji_task_id = metadata.get('labels', {}).get('koji-task-id')
        if koji_task_id is not None:
            self.log.info("build configuration created by Koji Task ID %s",
                          koji_task_id)
            extra['container_koji_task_id'] = koji_task_id

        fs_result = self.workflow.prebuild_results.get(AddFilesystemPlugin.key)
        if fs_result is not None:
            try:
                task_id = fs_result['filesystem-koji-task-id']
            except KeyError:
                self.log.error("%s: expected filesystem-koji-task-id in result",
                               AddFilesystemPlugin.key)
            else:
                extra['filesystem_koji_task_id'] = str(task_id)

        build = {
            'name': component,
            'version': version,
            'release': release,
            'source': "{0}#{1}".format(source.uri, source.commit_id),
            'start_time': start_time,
            'end_time': int(time.time()),
            'extra': extra,
        }

        if self.metadata_only:
            build['metadata_only'] = True

        return build

    def get_metadata(self):
        """
        Build the metadata needed for importing the build

        :return: tuple, the metadata and the list of Output instances
        """
        try:
            metadata = get_build_json()["metadata"]
            self.build_id = metadata["name"]
        except KeyError:
            self.log.error("No build metadata")
            raise

        for image in self.workflow.tag_conf.primary_images:
            # dash at first/last postition does not count
            if '-' in image.tag[1:-1]:
                self.nvr_image = image
                break
        else:
            raise RuntimeError('Unable to determine name:version-release')

        metadata_version = 0

        build = self.get_build(metadata)
        buildroot = self.get_buildroot(build_id=self.build_id)
        output_files = self.get_output(buildroot['id'])

        koji_metadata = {
            'metadata_version': metadata_version,
            'build': build,
            'buildroots': [buildroot],
            'output': [output.metadata for output in output_files],
        }

        return koji_metadata, output_files

    def upload_file(self, session, output, serverdir):
        """
        Upload a file to koji

        :return: str, pathname on server
        """
        name = output.metadata['filename']
        self.log.debug("uploading %r to %r as %r",
                       output.file.name, serverdir, name)

        kwargs = {}
        if self.blocksize is not None:
            kwargs['blocksize'] = self.blocksize
            self.log.debug("using blocksize %d", self.blocksize)

        upload_logger = KojiUploadLogger(self.log)
        session.uploadWrapper(output.file.name, serverdir, name=name,
                              callback=upload_logger.callback, **kwargs)
        path = os.path.join(serverdir, name)
        self.log.debug("uploaded %r", path)
        return path

    @staticmethod
    def get_upload_server_dir():
        """
        Create a path name for uploading files to

        :return: str, path name expected to be unique
        """
        dir_prefix = 'koji-promote'
        random_chars = ''.join([random.choice(ascii_letters)
                                for _ in range(8)])
        unique_fragment = '%r.%s' % (time.time(), random_chars)
        return os.path.join(dir_prefix, unique_fragment)

    def login(self):
        """
        Log in to koji

        :return: koji.ClientSession instance, logged in
        """
        auth_info = {
            "proxyuser": self.koji_proxy_user,
            "ssl_certs_dir": self.koji_ssl_certs,
            "krb_principal": self.koji_principal,
            "krb_keytab": self.koji_keytab
        }
        return create_koji_session(self.kojihub, auth_info)

    def run(self):
        """
        Run the plugin.
        """

        if ((self.koji_principal and not self.koji_keytab) or
                (self.koji_keytab and not self.koji_principal)):
            raise RuntimeError("specify both koji_principal and koji_keytab "
                               "or neither")

        # Only run if the build was successful
        if self.workflow.build_process_failed:
            self.log.info("Not promoting failed build to koji")
            return

        koji_metadata, output_files = self.get_metadata()

        try:
            session = self.login()
            server_dir = self.get_upload_server_dir()
            for output in output_files:
                if output.file:
                    self.upload_file(session, output, server_dir)
        finally:
            for output in output_files:
                if output.file:
                    output.file.close()

        try:
            build_info = session.CGImport(koji_metadata, server_dir)
        except Exception:
            self.log.debug("metadata: %r", koji_metadata)
            raise

        # Older versions of CGImport do not return a value.
        build_id = build_info.get("id") if build_info else None

        self.log.debug("Build information: %s",
                       json.dumps(build_info, sort_keys=True, indent=4))

        # Tag the build
        if build_id is not None and self.target is not None:
            self.log.debug("Finding build tag for target %s", self.target)
            target_info = session.getBuildTarget(self.target)
            build_tag = target_info['dest_tag_name']
            self.log.info("Tagging build with %s", build_tag)
            task_id = session.tagBuild(build_tag, build_id)
            task = TaskWatcher(session, task_id,
                               poll_interval=self.poll_interval)
            task.wait()
            if task.failed():
                raise RuntimeError("Task %s failed to tag koji build" % task_id)

        return build_id
