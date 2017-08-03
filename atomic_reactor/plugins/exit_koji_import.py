"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import json
import time

from atomic_reactor import start_time as atomic_reactor_start_time
from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.source import GitSource
from atomic_reactor.plugins.build_orchestrate_build import (get_worker_build_info,
                                                            get_koji_upload_dir)
from atomic_reactor.plugins.post_fetch_worker_metadata import FetchWorkerMetadataPlugin
from atomic_reactor.plugins.pre_add_filesystem import AddFilesystemPlugin
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild
from atomic_reactor.constants import PLUGIN_KOJI_IMPORT_PLUGIN_KEY
from atomic_reactor.util import (get_build_json, get_preferred_label, df_parser)
from atomic_reactor.koji_util import create_koji_session
from osbs.conf import Configuration
from osbs.api import OSBS


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
        :param target: str, koji target
        :param poll_interval: int, seconds between Koji task status requests
        """
        super(KojiImportPlugin, self).__init__(tasker, workflow)

        self.kojihub = kojihub
        self.koji_ssl_certs = koji_ssl_certs
        self.koji_proxy_user = koji_proxy_user

        self.koji_principal = koji_principal
        self.koji_keytab = koji_keytab

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
        for platform in worker_metadatas:
            for instance in worker_metadatas[platform]['output']:
                instance['buildroot_id'] = '{}-{}'.format(platform, instance['buildroot_id'])
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
        for platform in worker_metadatas:
            annotations = get_worker_build_info(self.workflow, platform).build.get_annotations()
            if annotations.get('media-types'):
                extra['image']['media_types'] = json.loads(annotations['media-types'])
                return

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
        try:
            metadata = get_build_json()["metadata"]
            self.build_id = metadata["name"]
        except KeyError:
            self.log.error("No build metadata")
            raise

        metadata_version = 0

        worker_metadatas = self.workflow.postbuild_results.get(FetchWorkerMetadataPlugin.key)
        build = self.get_build(metadata, worker_metadatas)
        buildroot = self.get_buildroot(worker_metadatas)
        output = self.get_output(worker_metadatas)

        koji_metadata = {
            'metadata_version': metadata_version,
            'build': build,
            'buildroots': buildroot,
            'output': output,
        }
        return koji_metadata

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

        koji_metadata = self.combine_metadata_fragments()

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
