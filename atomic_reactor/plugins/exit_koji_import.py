"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import json
import os
import time
from tempfile import NamedTemporaryFile

from atomic_reactor import start_time as atomic_reactor_start_time
from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.source import GitSource
from atomic_reactor.plugins.build_orchestrate_build import (get_worker_build_info,
                                                            get_koji_upload_dir)
from atomic_reactor.plugins.pre_add_filesystem import AddFilesystemPlugin
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild
from atomic_reactor.constants import (PLUGIN_KOJI_IMPORT_PLUGIN_KEY,
                                      PLUGIN_PULP_PULL_KEY,
                                      PLUGIN_PULP_SYNC_KEY,
                                      PLUGIN_FETCH_WORKER_METADATA_KEY,
                                      PLUGIN_GROUP_MANIFESTS_KEY)
from atomic_reactor.util import (get_build_json, get_preferred_label,
                                 df_parser, ImageName, get_checksums)
from atomic_reactor.koji_util import (create_koji_session, Output, KojiUploadLogger)
from osbs.conf import Configuration
from osbs.api import OSBS
from osbs.exceptions import OsbsException


class KojiImportPlugin(ExitPlugin):
    """
    Import this build to Koji

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

    Runs as an exit plugin in order to capture logs from all other
    plugins.
    """

    key = PLUGIN_KOJI_IMPORT_PLUGIN_KEY
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, kojihub, url,
                 verify_ssl=True, use_auth=True,
                 koji_ssl_certs=None, koji_proxy_user=None,
                 koji_principal=None, koji_keytab=None,
                 blocksize=None,
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
        :param blocksize: int, blocksize to use for uploading files
        :param target: str, koji target
        :param poll_interval: int, seconds between Koji task status requests
        """
        super(KojiImportPlugin, self).__init__(tasker, workflow)

        self.kojihub = kojihub
        self.koji_ssl_certs = koji_ssl_certs
        self.koji_proxy_user = koji_proxy_user

        self.koji_principal = koji_principal
        self.koji_keytab = koji_keytab

        self.blocksize = blocksize
        self.target = target
        self.poll_interval = poll_interval

        self.namespace = get_build_json().get('metadata', {}).get('namespace', None)
        osbs_conf = Configuration(conf_file=None, openshift_uri=url,
                                  use_auth=use_auth, verify_ssl=verify_ssl,
                                  namespace=self.namespace)
        self.osbs = OSBS(osbs_conf, osbs_conf)
        self.build_id = None

    def get_output(self, worker_metadatas):
        """
        Build the output entry of the metadata.

        :return: list, containing dicts of partial metadata
        """
        outputs = []
        has_pulp_pull = PLUGIN_PULP_PULL_KEY in self.workflow.exit_results
        try:
            pulp_sync_results = self.workflow.postbuild_results[PLUGIN_PULP_SYNC_KEY]
            crane_registry = pulp_sync_results[0]
        except (KeyError, IndexError):
            crane_registry = None

        for platform in worker_metadatas:
            for instance in worker_metadatas[platform]['output']:
                instance['buildroot_id'] = '{}-{}'.format(platform, instance['buildroot_id'])

                if instance['type'] == 'docker-image':
                    # update image ID with pulp_pull results
                    if platform == "x86_64" and has_pulp_pull:
                        exit_results = self.workflow.exit_results
                        image_id, _ = exit_results[PLUGIN_PULP_PULL_KEY]
                        if image_id is not None:
                            instance['extra']['docker']['id'] = image_id

                    # update repositories to point to Crane
                    if crane_registry:
                        pulp_pullspecs = []
                        docker = instance['extra']['docker']
                        for pullspec in docker['repositories']:
                            image = ImageName.parse(pullspec)
                            image.registry = crane_registry.registry
                            pulp_pullspecs.append(image.to_str())

                        docker['repositories'] = pulp_pullspecs

                outputs.append(instance)

        return outputs

    def get_buildroot(self, worker_metadatas):
        """
        Build the buildroot entry of the metadata.

        :return: list, containing dicts of partial metadata
        """
        buildroots = []
        for platform in sorted(worker_metadatas.keys()):
            for instance in worker_metadatas[platform]['buildroots']:
                instance['id'] = '{}-{}'.format(platform, instance['id'])
                buildroots.append(instance)

        return buildroots

    def get_logs(self):
        """
        Build list of log files

        :return: list, of log files
        """

        logs = None
        output = []

        # Collect logs from server
        try:
            logs = self.osbs.get_orchestrator_build_logs(self.build_id)
        except OsbsException as ex:
            self.log.error("unable to get build logs: %r", ex)
            return output
        except TypeError:
            # Older osbs-client has no get_orchestrator_build_logs
            self.log.error("OSBS client does not support get_orchestrator_build_logs")
            return output

        platform_logs = {}
        for entry in logs:
            platform = entry.platform
            if platform not in platform_logs:
                filename = 'orchestrator' if platform is None else platform
                platform_logs[platform] = NamedTemporaryFile(prefix="%s-%s" %
                                                             (self.build_id, filename),
                                                             suffix=".log", mode='wb')
            platform_logs[platform].write((entry.line + '\n').encode('utf-8'))

        for platform, logfile in platform_logs.items():
            logfile.flush()
            filename = 'orchestrator' if platform is None else platform
            metadata = self.get_output_metadata(logfile.name, "%s.log" % filename)
            output.append(Output(file=logfile, metadata=metadata))

        return output


    def set_help(self, extra, worker_metadatas):
        all_annotations = [get_worker_build_info(self.workflow, platform).build.get_annotations()
                           for platform in worker_metadatas]
        help_known = ['help_file' in annotations for annotations in all_annotations]
        # Only set the 'help' key when any 'help_file' annotation is set
        if any(help_known):
            # See if any are not None
            for known, annotations in zip(help_known, all_annotations):
                if known:
                    help_file = json.loads(annotations['help_file'])
                    if help_file is not None:
                        extra['image']['help'] = help_file
                        break
            else:
                # They are all None
                extra['image']['help'] = None

    def set_media_types(self, extra, worker_metadatas):
        media_types = []
        for platform in worker_metadatas:
            annotations = get_worker_build_info(self.workflow, platform).build.get_annotations()
            if annotations.get('media-types'):
                media_types = json.loads(annotations['media-types'])
                break

        # Append media_types from pulp pull
        pulp_pull_results = self.workflow.exit_results.get(PLUGIN_PULP_PULL_KEY)
        if pulp_pull_results:
            media_types += pulp_pull_results[1]

        if media_types:
            extra['image']['media_types'] = sorted(list(set(media_types)))

    def set_group_manifest_info(self, extra, worker_metadatas):
        manifest_list_digests = self.workflow.postbuild_results.get(PLUGIN_GROUP_MANIFESTS_KEY)
        if manifest_list_digests:
            index = {}
            index['tags'] = [image.tag for image in self.workflow.tag_conf.images]
            repositories = self.workflow.build_result.annotations['repositories']['unique']
            repo = ImageName.parse(repositories[0]).to_str(registry=False, tag=False)
            # group_manifests added the registry, so this should be valid
            registries = self.workflow.push_conf.pulp_registries
            if not registries:
                registries = self.workflow.push_conf.all_registries
            for registry in registries:
                pullspec = "{0}/{1}@{2}".format(registry.uri, repo,
                                                manifest_list_digests[0].v2_list)
                index['pull'] = [pullspec]
                for tag in index['tags']:
                    if '-' in tag:  # {version}-{release} only, and only one instance
                        pullspec = "{0}/{1}:{2}".format(registry.uri, repo, tag)
                        index['pull'].append(pullspec)
                        break
                break
            extra['image']['index'] = index
        # group_manifests returns None if didn't run, [] if group=False
        elif manifest_list_digests == []:
            for platform in worker_metadatas:
                if platform == "x86_64":
                    for instance in worker_metadatas[platform]['output']:
                        if instance['type'] == 'docker-image':
                            # koji_upload, running in the worker, doesn't have the full tags
                            # so set them here
                            tags = [image.tag for image in self.workflow.tag_conf.images]
                            instance['extra']['docker']['tags'] = tags
                            self.log.debug("reset tags to so that docker is %s",
                                           instance['extra']['docker'])

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

    def get_build(self, metadata, worker_metadatas):
        start_time = int(atomic_reactor_start_time)

        labels = df_parser(self.workflow.builder.df_path, workflow=self.workflow).labels

        component = get_preferred_label(labels, 'com.redhat.component')
        version = get_preferred_label(labels, 'version')
        release = get_preferred_label(labels, 'release')

        source = self.workflow.source
        if not isinstance(source, GitSource):
            raise RuntimeError('git source required')

        extra = {'image': {'autorebuild': is_rebuild(self.workflow)}}
        koji_task_id = metadata.get('labels', {}).get('koji-task-id')
        if koji_task_id is not None:
            self.log.info("build configuration created by Koji Task ID %s",
                          koji_task_id)
            try:
                extra['container_koji_task_id'] = int(koji_task_id)
            except ValueError:
                self.log.error("invalid task ID %r", koji_task_id, exc_info=1)

        fs_result = self.workflow.prebuild_results.get(AddFilesystemPlugin.key)
        if fs_result is not None:
            try:
                fs_task_id = fs_result['filesystem-koji-task-id']
            except KeyError:
                self.log.error("%s: expected filesystem-koji-task-id in result",
                               AddFilesystemPlugin.key)
            else:
                try:
                    task_id = int(fs_task_id)
                except ValueError:
                    self.log.error("invalid task ID %r", fs_task_id, exc_info=1)
                else:
                    extra['filesystem_koji_task_id'] = task_id

        self.set_help(extra, worker_metadatas)
        self.set_media_types(extra, worker_metadatas)
        self.set_group_manifest_info(extra, worker_metadatas)

        build = {
            'name': component,
            'version': version,
            'release': release,
            'source': "{0}#{1}".format(source.uri, source.commit_id),
            'start_time': start_time,
            'end_time': int(time.time()),
            'extra': extra,
        }

        return build

    def combine_metadata_fragments(self):
        def add_buildroot_id(output, buildroot_id):
            logfile, metadata = output
            metadata.update({'buildroot_id': buildroot_id})
            return Output(file=logfile, metadata=metadata)

        def add_log_type(output):
            logfile, metadata = output
            metadata.update({'type': 'log', 'arch': 'noarch'})
            return Output(file=logfile, metadata=metadata)

        try:
            metadata = get_build_json()["metadata"]
            self.build_id = metadata["name"]
        except KeyError:
            self.log.error("No build metadata")
            raise

        metadata_version = 0

        worker_metadatas = self.workflow.postbuild_results.get(PLUGIN_FETCH_WORKER_METADATA_KEY)
        build = self.get_build(metadata, worker_metadatas)
        buildroot = self.get_buildroot(worker_metadatas)
        buildroot_id = buildroot[0]['id']
        output = self.get_output(worker_metadatas)
        output_files = [add_log_type(add_buildroot_id(md, buildroot_id))
                        for md in self.get_logs()]
        output.extend([of.metadata for of in output_files])

        koji_metadata = {
            'metadata_version': metadata_version,
            'build': build,
            'buildroots': buildroot,
            'output': output,
        }
        return koji_metadata, output_files

    def login(self):
        """
        Log in to koji

        :return: koji.ClientSession instance, logged in
        """

        # krbV python library throws an error if these are unicode
        auth_info = {
            "proxyuser": self.koji_proxy_user,
            "ssl_certs_dir": self.koji_ssl_certs,
            "krb_principal": str(self.koji_principal),
            "krb_keytab": str(self.koji_keytab)
        }
        return create_koji_session(str(self.kojihub), auth_info)

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

        if ((self.koji_principal and not self.koji_keytab) or
                (self.koji_keytab and not self.koji_principal)):
            raise RuntimeError("specify both koji_principal and koji_keytab "
                               "or neither")

        # Only run if the build was successful
        if self.workflow.build_process_failed:
            self.log.info("Not importing failed build to koji")
            return

        session = self.login()

        server_dir = get_koji_upload_dir(self.workflow)

        koji_metadata, output_files = self.combine_metadata_fragments()

        try:
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

        return build_id
