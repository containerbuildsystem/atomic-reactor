"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from collections import namedtuple
import os
import subprocess
from tempfile import NamedTemporaryFile
import copy

from atomic_reactor import __version__ as atomic_reactor_version
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin
from atomic_reactor.constants import PROG, PLUGIN_KOJI_UPLOAD_PLUGIN_KEY
from atomic_reactor.util import (get_version_of_tools, get_checksums,
                                 get_build_json, get_docker_architecture)
from atomic_reactor.koji_util import create_koji_session
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

    def callback(self, offset, totalsize, size, t1, t2):  # pylint: disable=W0613
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


class KojiUploadPlugin(PostBuildPlugin):
    """
    Upload this build to Koji

    Note: only the image archive is uploaded to Koji at this stage.
    Metadata about this image is created and stored in a ConfigMap in
    OpenShift, ready for the orchestrator build to collect and use to
    actually create the Koji Build together with the uploaded image
    archive(s).

    Authentication is with Kerberos unless the koji_ssl_certs_dir
    configuration parameter is given, in which case it should be a
    path at which 'cert', 'ca', and 'serverca' are the certificates
    for SSL authentication.

    If Kerberos is used for authentication, the default principal will
    be used (from the kernel keyring) unless both koji_keytab and
    koji_principal are specified. The koji_keytab parameter is a
    keytab name like 'type:name', and so can be used to specify a key
    in a Kubernetes secret by specifying 'FILE:/path/to/key'.
    """

    key = PLUGIN_KOJI_UPLOAD_PLUGIN_KEY
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, kojihub, url, build_json_dir,
                 koji_upload_dir, verify_ssl=True, use_auth=True,
                 koji_ssl_certs_dir=None, koji_proxy_user=None,
                 koji_principal=None, koji_keytab=None,
                 blocksize=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param kojihub: string, koji hub (xmlrpc)
        :param url: string, URL for OSv3 instance
        :param build_json_dir: str, path to directory with input json
        :param koji_upload_dir: str, path to use when uploading to hub
        :param verify_ssl: bool, verify OSv3 SSL certificate?
        :param use_auth: bool, initiate authentication with OSv3?
        :param koji_ssl_certs_dir: str, path to 'cert', 'ca', 'serverca'
        :param koji_proxy_user: str, user to log in as (requires hub config)
        :param koji_principal: str, Kerberos principal (must specify keytab)
        :param koji_keytab: str, keytab name (must specify principal)
        :param blocksize: int, blocksize to use for uploading files
        """
        super(KojiUploadPlugin, self).__init__(tasker, workflow)

        self.kojihub = kojihub
        self.koji_ssl_certs_dir = koji_ssl_certs_dir
        self.koji_proxy_user = koji_proxy_user

        self.koji_principal = koji_principal
        self.koji_keytab = koji_keytab

        self.blocksize = blocksize
        self.build_json_dir = build_json_dir
        self.koji_upload_dir = koji_upload_dir

        self.namespace = get_build_json().get('metadata', {}).get('namespace', None)
        osbs_conf = Configuration(conf_file=None, openshift_uri=url,
                                  use_auth=use_auth, verify_ssl=verify_ssl,
                                  build_json_dir=self.build_json_dir,
                                  namespace=self.namespace)
        self.osbs = OSBS(osbs_conf, osbs_conf)
        self.build_id = None
        self.pullspec_image = None

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

        docker_info = self.tasker.get_info()
        host_arch, docker_version = get_docker_architecture(self.tasker)

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
                    'version': docker_version,
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
                                         mode='wb')
            try:
                logfile.write(logs)
            except (TypeError, UnicodeEncodeError):
                # Older osbs-client versions returned Unicode objects
                logfile.write(logs.encode('utf-8'))
            logfile.flush()
            metadata = self.get_output_metadata(logfile.name,
                                                "openshift-final.log")
            output.append(Output(file=logfile, metadata=metadata))

        docker_logs = NamedTemporaryFile(prefix="docker-%s" % self.build_id,
                                         suffix=".log",
                                         mode='wb')
        docker_logs.write("\n".join(self.workflow.build_result.logs).encode('utf-8'))
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

        try:
            sep = PostBuildRPMqaPlugin.sep
        except AttributeError:
            # sep instance variable added in Aug 2016
            sep = ','

        return self.parse_rpm_output(output, PostBuildRPMqaPlugin.rpm_tags,
                                     separator=sep)

    def get_image_output(self, arch):
        """
        Create the output for the image

        This is the Koji Content Generator metadata, along with the
        'docker save' output to upload.

        :param arch: str, architecture for this output
        :return: tuple, (metadata dict, Output instance)

        """

        image_id = self.workflow.builder.image_id
        saved_image = self.workflow.exported_image_sequence[-1].get('path')
        ext = saved_image.split('.', 1)[1]
        name_fmt = 'docker-image-{id}.{arch}.{ext}'
        image_name = name_fmt.format(id=image_id, arch=arch, ext=ext)
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
                    # pulp/crane supports only manifest schema v1
                    if self.workflow.push_conf.pulp_registries:
                        digest = registry.digests[image_str].v1
                    else:
                        digest = registry.digests[image_str].default
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
            image = self.pullspec_image.copy()
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

        def add_log_type(output, arch):
            logfile, metadata = output
            metadata.update({'type': 'log', 'arch': arch})
            return Output(file=logfile, metadata=metadata)

        arch = os.uname()[4]
        output_files = [add_log_type(add_buildroot_id(metadata), arch)
                        for metadata in self.get_logs()]

        # Parent of squashed built image is base image
        image_id = self.workflow.builder.image_id
        parent_id = self.workflow.base_image_inspect['Id']

        # Read config from the registry using v2 schema 2 digest
        registries = self.workflow.push_conf.docker_registries
        if registries:
            config = copy.deepcopy(registries[0].config)
        else:
            config = {}

        # We don't need container_config section
        if config and 'container_config' in config:
            del config['container_config']

        digests = self.get_digests()
        repositories = self.get_repositories(digests)
        tags = set(image.tag for image in self.workflow.tag_conf.primary_images)
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
                    'tags': list(tags),
                    'config': config
                },
            },
        })

        if not config:
            del metadata['extra']['docker']['config']

        # Add the 'docker save' image to the output
        image = add_buildroot_id(output)
        output_files.append(image)

        return output_files

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

        for image in self.workflow.tag_conf.unique_images:
            self.pullspec_image = image
            break

        for image in self.workflow.tag_conf.primary_images:
            # dash at first/last postition does not count
            if '-' in image.tag[1:-1]:
                self.pullspec_image = image
                break

        if not self.pullspec_image:
            raise RuntimeError('Unable to determine pullspec_image')

        metadata_version = 0

        buildroot = self.get_buildroot(build_id=self.build_id)
        output_files = self.get_output(buildroot['id'])

        koji_metadata = {
            'metadata_version': metadata_version,
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

    def login(self):
        """
        Log in to koji

        :return: koji.ClientSession instance, logged in
        """

        # krbV python library throws an error if these are unicode
        auth_info = {
            "proxyuser": self.koji_proxy_user,
            "ssl_certs_dir": self.koji_ssl_certs_dir,
            "krb_principal": str(self.koji_principal),
            "krb_keytab": str(self.koji_keytab)
        }
        return create_koji_session(str(self.kojihub), auth_info)

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
            for output in output_files:
                if output.file:
                    self.upload_file(session, output, self.koji_upload_dir)
        finally:
            for output in output_files:
                if output.file:
                    output.file.close()

        md_fragment = "{}-md".format(get_build_json()['metadata']['name'])
        md_fragment_key = 'metadata.json'
        cm_data = {md_fragment_key: koji_metadata}
        annotations = {
            "metadata_fragment": "configmap/" + md_fragment,
            "metadata_fragment_key": md_fragment_key
        }

        try:
            self.osbs.create_config_map(md_fragment, cm_data)
        except OsbsException:
            self.log.debug("metadata: %r", koji_metadata)
            self.log.debug("annotations: %r", annotations)
            raise

        return annotations
