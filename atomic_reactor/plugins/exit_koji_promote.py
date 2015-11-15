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

import koji
from atomic_reactor import __version__ as atomic_reactor_version
from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.source import GitSource
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild
from atomic_reactor.constants import PROG
from atomic_reactor.util import get_version_of_tools, get_checksums
from osbs.conf import Configuration
from osbs.api import OSBS

# An output file and its metadata
Output = namedtuple('Output', ['file', 'metadata'])


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

    If metadata_only is set, the v1 image will not be uploaded, only
    the logs. The import will be marked as metadata-only.

    Runs as an exit plugin in order to capture logs from all other
    plugins.
    """

    key = "koji_promote"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, kojihub, url,
                 verify_ssl=True, use_auth=True,
                 koji_ssl_certs=None, koji_proxy_user=None,
                 koji_principal=None, koji_keytab=None,
                 metadata_only=False):
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
        :param metadata_only: bool, whether to omit the v1 image
        """
        super(KojiPromotePlugin, self).__init__(tasker, workflow)

        self.kojihub = kojihub
        self.koji_ssl_certs = koji_ssl_certs
        self.koji_proxy_user = koji_proxy_user
        self.koji_principal = koji_principal
        self.koji_keytab = koji_keytab
        self.metadata_only = metadata_only

        osbs_conf = Configuration(conf_file=None, openshift_uri=url,
                                  use_auth=use_auth, verify_ssl=verify_ssl)
        self.osbs = OSBS(osbs_conf, osbs_conf)
        self.build_id = None
        self.namespace = None

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

        buildroot_tag = os.environ["OPENSHIFT_CUSTOM_BUILD_BASE_IMAGE"]
        kwargs = {}
        if self.namespace is not None:
            kwargs['namespace'] = self.namespace
        pod = self.osbs.get_pod_for_build(self.build_id, **kwargs)
        all_images = pod.get_container_image_ids()

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

        # Collect logs from server
        kwargs = {}
        if self.namespace is not None:
            kwargs['namespace'] = self.namespace
        logs = self.osbs.get_build_logs(self.build_id, **kwargs)

        # Deleted once closed
        logfile = NamedTemporaryFile(prefix=self.build_id,
                                     suffix=".log",
                                     mode='w')
        logfile.write(logs)
        logfile.flush()

        docker_logs = NamedTemporaryFile(prefix="docker-%s" % self.build_id,
                                         suffix=".log",
                                         mode='w')
        docker_logs.write("\n".join(self.workflow.build_logs))
        docker_logs.flush()

        return [Output(file=docker_logs,
                       metadata=self.get_output_metadata(docker_logs.name,
                                                         "build.log")),
                Output(file=logfile,
                       metadata=self.get_output_metadata(logfile.name,
                                                         "openshift-final.log"))]

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

    def get_image_output(self):
        """
        Create the output for the image

        For v1, this is the v1 image. For v2, this is the v2 metadata
        with the checksum of an empty file, and no actual upload.

        :return: tuple, (metadata dict, Output instance)
        """

        image_id = self.workflow.builder.image_id
        v1_image = self.workflow.exported_image_sequence[-1].get('path')
        ext = v1_image.split('.', 1)[1]
        if self.metadata_only:
            v2_image_name = 'docker-v2-image-{0}.{1}'.format(image_id, ext)
            metadata = self.get_output_metadata(os.path.devnull, v2_image_name)
            output = Output(file=None, metadata=metadata)
        else:
            v1_image_name = 'docker-v1-image-{0}.{1}'.format(image_id, ext)
            metadata = self.get_output_metadata(v1_image, v1_image_name)
            output = Output(file=open(v1_image), metadata=metadata)

        return metadata, output

    def get_output_images(self):
        if self.workflow.push_conf.pulp_registries:
            # If pulp was used, only report pulp images
            registries = self.workflow.push_conf.pulp_registries
        else:
            # Otherwise report all the images we pushed
            registries = self.workflow.push_conf.all_registries

        output_images = []
        for registry in registries:
            for image in (self.workflow.tag_conf.primary_images +
                          self.workflow.tag_conf.unique_images):
                registry_image = image.copy()
                registry_image.registry = registry.uri
                if registry_image not in output_images:
                    output_images.append(registry_image)

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
        output_images = self.get_output_images()
        repositories = [image.to_str() for image in output_images
                        if image.tag != 'latest']
        arch = os.uname()[4]
        metadata, output = self.get_image_output()
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

        # Add the v1 image (or v2 metadata) to the output
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

        name = None
        version = None
        release = None
        for image_name in self.workflow.tag_conf.primary_images:
            if '-' in image_name.tag:
                name = image_name.to_str(registry=False, tag=False)
                version, release = image_name.tag.split('-', 1)

        if name is None or version is None or release is None:
            raise RuntimeError('Unable to determine name-version-release')

        source = self.workflow.source
        if not isinstance(source, GitSource):
            raise RuntimeError('git source required')

        build = {
            'name': name,
            'version': version,
            'release': release,
            'source': "{0}#{1}".format(source.uri, source.commit_id),
            'start_time': start_time,
            'end_time': int(time.time()),
            'extra': {
                'image': {},
            },
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
            build_json = json.loads(os.environ["BUILD"])
        except KeyError:
            self.log.error("No $BUILD env variable. "
                           "Probably not running in build container.")
            raise

        try:
            metadata = build_json["metadata"]
            self.build_id = metadata["name"]
            self.namespace = metadata.get("namespace")
        except KeyError:
            self.log.error("No build metadata")
            raise

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
        session.uploadWrapper(output.file.name, serverdir, name=name)
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
        session = koji.ClientSession(self.kojihub)
        kwargs = {}
        if self.koji_proxy_user:
            kwargs['proxyuser'] = self.koji_proxy_user

        if self.koji_ssl_certs:
            # Use certificates
            self.log.info("Using SSL certificates for Koji authentication")
            session.ssl_login(os.path.join(self.koji_ssl_certs, 'cert'),
                              os.path.join(self.koji_ssl_certs, 'ca'),
                              os.path.join(self.koji_ssl_certs, 'serverca'),
                              **kwargs)
        else:
            # Use Kerberos
            self.log.info("Using Kerberos for Koji authentication")
            if self.koji_principal and self.koji_keytab:
                kwargs['principal'] = self.koji_principal
                kwargs['keytab'] = self.koji_keytab

            session.krb_login(**kwargs)

        return session

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

        if not is_rebuild(self.workflow):
            self.log.info("Not promoting to koji: not a rebuild")
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

        session.CGImport(koji_metadata, server_dir)

        self.log.debug("Submitted with metadata: %s",
                       json.dumps(koji_metadata, sort_keys=True, indent=4))
