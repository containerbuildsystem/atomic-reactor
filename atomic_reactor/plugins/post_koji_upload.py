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
from atomic_reactor.plugins.pre_reactor_config import (get_openshift_session,
                                                       get_prefer_schema1_digest,
                                                       get_koji_session, get_pulp)
from atomic_reactor.constants import (PROG, PLUGIN_KOJI_UPLOAD_PLUGIN_KEY,
                                      PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY,
                                      OPERATOR_MANIFESTS_ARCHIVE)
from atomic_reactor.util import (get_version_of_tools, get_checksums,
                                 get_build_json, get_docker_architecture,
                                 get_image_upload_filename,
                                 get_manifest_media_type, ImageName, is_scratch_build)
from atomic_reactor.rpm_util import parse_rpm_output, rpm_qf_args
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

    def __init__(self, tasker, workflow, koji_upload_dir, kojihub=None, url=None,
                 build_json_dir=None, verify_ssl=True, use_auth=True,
                 koji_ssl_certs_dir=None, koji_proxy_user=None,
                 koji_principal=None, koji_keytab=None,
                 blocksize=None, prefer_schema1_digest=True,
                 platform='x86_64', report_multiple_digests=False):
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
        :param prefer_schema1_digest: bool, when True, v2 schema 1 digest will
            be preferred as the built image digest
        :param platform: str, platform name for this build
        :param report_multiple_digests: bool, whether to report both schema 1
            and schema 2 digests; if truthy, prefer_schema1_digest is ignored
        """
        super(KojiUploadPlugin, self).__init__(tasker, workflow)

        self.koji_fallback = {
            'hub_url': kojihub,
            'auth': {
                'proxyuser': koji_proxy_user,
                'ssl_certs_dir': koji_ssl_certs_dir,
                'krb_principal': str(koji_principal),
                'krb_keytab_path': str(koji_keytab)
            }
        }

        self.openshift_fallback = {
            'url': url,
            'insecure': not verify_ssl,
            'auth': {'enable': use_auth},
            'build_json_dir': build_json_dir
        }

        self.blocksize = blocksize
        self.koji_upload_dir = koji_upload_dir
        self.prefer_schema1_digest = get_prefer_schema1_digest(self.workflow, prefer_schema1_digest)
        self.report_multiple_digests = report_multiple_digests

        self.osbs = get_openshift_session(self.workflow, self.openshift_fallback)
        self.build_id = None
        self.pullspec_image = None
        self.platform = platform

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

        cmd = "/bin/rpm " + rpm_qf_args(tags)
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

        return parse_rpm_output(output.splitlines(), tags)

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

        build_logs = NamedTemporaryFile(prefix="buildstep-%s" % self.build_id,
                                        suffix=".log",
                                        mode='wb')
        build_logs.write("\n".join(self.workflow.build_result.logs).encode('utf-8'))
        build_logs.flush()
        filename = "{platform}-build.log".format(platform=self.platform)
        return [Output(file=build_logs,
                       metadata=self.get_output_metadata(build_logs.name,
                                                         filename))]

    def get_image_components(self):
        """
        Re-package the output of the rpmqa plugin into the format required
        for the metadata.
        """

        output = self.workflow.image_components
        if output is None:
            self.log.error("%s plugin did not run!",
                           PostBuildRPMqaPlugin.key)
            output = []

        return output

    def get_image_output(self):
        """
        Create the output for the image

        This is the Koji Content Generator metadata, along with the
        'docker save' output to upload.

        :return: tuple, (metadata dict, Output instance)

        """

        saved_image = self.workflow.exported_image_sequence[-1].get('path')
        image_name = get_image_upload_filename(self.workflow.exported_image_sequence[-1],
                                               self.workflow.builder.image_id,
                                               self.platform)
        metadata = self.get_output_metadata(saved_image, image_name)
        output = Output(file=open(saved_image), metadata=metadata)

        return metadata, output

    def select_digest(self, digests):
        digest = digests.default

        # pulp/crane supports only manifest schema v1
        if self.prefer_schema1_digest:
            if self.workflow.push_conf.pulp_registries:
                self.log.info('Using schema v1 digest because of older Pulp integration')
                digest = digests.v1
            else:
                self.log.info('Schema v1 preferred, but not used')

        return digest

    def get_repositories_and_digests(self):
        """
        Returns a map of images to their repositories and a map of media types to each digest

        it creates a map of images to digests, which is need to create the image->repository
        map and uses the same loop structure as media_types->digest, but the image->digest
        map isn't needed after we have the image->repository map and can be discarded.
        """
        digests = {}  # image -> digests
        typed_digests = {}  # media_type -> digests
        for registry in self.workflow.push_conf.docker_registries:
            for image in self.workflow.tag_conf.images:
                image_str = image.to_str()
                if image_str in registry.digests:
                    image_digests = registry.digests[image_str]
                    if self.report_multiple_digests and get_pulp(self.workflow, None):
                        digest_list = [digest for digest in (image_digests.v1,
                                                             image_digests.v2)
                                       if digest]
                    else:
                        digest_list = [self.select_digest(image_digests)]
                    digests[image.to_str(registry=False)] = digest_list
                    for digest_version in image_digests.content_type:
                        if digest_version not in image_digests:
                            continue
                        if not get_pulp(self.workflow, None) and digest_version == 'v1':
                            continue
                        digest_type = get_manifest_media_type(digest_version)
                        typed_digests[digest_type] = image_digests[digest_version]

        if self.workflow.push_conf.pulp_registries:
            # If pulp was used, only report pulp images
            registries = self.workflow.push_conf.pulp_registries
        else:
            # Otherwise report all the images we pushed
            registries = self.workflow.push_conf.all_registries
        repositories = []
        for registry in registries:
            image = self.pullspec_image.copy()
            image.registry = registry.uri
            pullspec = image.to_str()

            repositories.append(pullspec)

            digest_list = digests.get(image.to_str(registry=False), ())
            for digest in digest_list:
                digest_pullspec = image.to_str(tag=False) + "@" + digest
                repositories.append(digest_pullspec)

        return repositories, typed_digests

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
        parent_id = None
        if not self.workflow.builder.base_from_scratch:
            parent_id = self.workflow.builder.base_image_inspect['Id']

        # Read config from the registry using v2 schema 2 digest
        registries = self.workflow.push_conf.docker_registries
        if registries:
            config = copy.deepcopy(registries[0].config)
        else:
            config = {}

        # We don't need container_config section
        if config and 'container_config' in config:
            del config['container_config']

        repositories, typed_digests = self.get_repositories_and_digests()
        tags = set(image.tag for image in self.workflow.tag_conf.images)
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
                    'layer_sizes': self.workflow.layer_sizes,
                    'tags': list(tags),
                    'config': config,
                    'digests': typed_digests
                },
            },
        })

        if self.workflow.builder.base_from_scratch:
            del metadata['extra']['docker']['parent_id']
        if not config:
            del metadata['extra']['docker']['config']
        if not typed_digests:
            del metadata['extra']['docker']['digests']

        # Add the 'docker save' image to the output
        image = add_buildroot_id(output)
        output_files.append(image)

        # add operator manifests to output
        operator_manifests_path = (self.workflow.postbuild_results
                                   .get(PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY))
        if operator_manifests_path:
            operator_manifests_file = open(operator_manifests_path)
            manifests_metadata = self.get_output_metadata(operator_manifests_path,
                                                          OPERATOR_MANIFESTS_ARCHIVE)
            operator_manifests_output = Output(file=operator_manifests_file,
                                               metadata=manifests_metadata)
            # We use log type here until a more appropriate type name is supported by koji
            operator_manifests_output.metadata.update({'arch': arch, 'type': 'log'})
            operator_manifests = add_buildroot_id(operator_manifests_output)
            output_files.append(operator_manifests)

        return output_files

    def update_buildroot_koji(self, buildroot, output):
        """
        put the final koji information in the buildroot under extra.osbs
        """
        docker = output[1]['extra']['docker']

        name = ''
        for tag in docker['tags']:
            for repo in docker['repositories']:
                if tag in repo:
                    iname = ImageName.parse(repo)
                    name = iname.to_str(registry=False)
                    break

        buildroot['extra']['osbs']['koji'] = {
            'build_name': name,
            'builder_image_id': docker.get('digests', {})
        }

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

        output = [output.metadata for output in output_files]
        koji_metadata = {
            'metadata_version': metadata_version,
            'buildroots': [buildroot],
            'output': output,
        }
        self.update_buildroot_koji(buildroot, output)

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

    def run(self):
        """
        Run the plugin.
        """
        # Only run if the build was successful
        if self.workflow.build_process_failed:
            self.log.info("Not promoting failed build to koji")
            return

        koji_metadata, output_files = self.get_metadata()

        if not is_scratch_build():
            try:
                session = get_koji_session(self.workflow, self.koji_fallback)
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
