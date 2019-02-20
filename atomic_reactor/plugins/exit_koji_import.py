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
import logging
from tempfile import NamedTemporaryFile

from atomic_reactor import start_time as atomic_reactor_start_time
from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.source import GitSource
from atomic_reactor.plugins.build_orchestrate_build import (get_worker_build_info,
                                                            get_koji_upload_dir)
from atomic_reactor.plugins.pre_add_filesystem import AddFilesystemPlugin
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild
from atomic_reactor.util import OSBSLogs, get_parent_image_koji_data
from atomic_reactor.plugins.pre_reactor_config import get_openshift_session

try:
    from atomic_reactor.plugins.pre_flatpak_create_dockerfile import get_flatpak_source_info
    from atomic_reactor.plugins.pre_resolve_module_compose import get_compose_info
except ImportError:
    # modulemd not available
    def get_flatpak_source_info(_):
        return None

try:
    from atomic_reactor.plugins.post_pulp_sync import get_manifests_in_pulp_repository
except ImportError:
    # no dockpulp available
    def get_manifests_in_pulp_repository(_):
        raise KeyError

from atomic_reactor.constants import (
    PLUGIN_KOJI_IMPORT_PLUGIN_KEY, PLUGIN_PULP_PULL_KEY, PLUGIN_PULP_SYNC_KEY,
    PLUGIN_FETCH_WORKER_METADATA_KEY, PLUGIN_GROUP_MANIFESTS_KEY, PLUGIN_RESOLVE_COMPOSES_KEY,
    PLUGIN_VERIFY_MEDIA_KEY, METADATA_TAG, OPERATOR_MANIFESTS_ARCHIVE)
from atomic_reactor.util import (Output, get_build_json,
                                 df_parser, ImageName, get_primary_images,
                                 get_manifest_media_type,
                                 get_digests_map_from_annotations, is_scratch_build)
from atomic_reactor.koji_util import (KojiUploadLogger, get_koji_task_owner)
from atomic_reactor.plugins.pre_reactor_config import get_koji_session
from osbs.utils import Labels


class KojiImportPlugin(ExitPlugin):
    """
    Import this build to Koji

    Submits a successful build to Koji using the Content Generator API,
    https://docs.pagure.org/koji/content_generators

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

    def __init__(self, tasker, workflow, kojihub=None, url=None,
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

        self.koji_fallback = {
            'hub_url': kojihub,
            'auth': {
                'proxyuser': koji_proxy_user,
                'ssl_certs_dir': koji_ssl_certs,
                'krb_principal': str(koji_principal),
                'krb_keytab_path': str(koji_keytab)
            }
        }

        self.openshift_fallback = {
            'url': url,
            'insecure': not verify_ssl,
            'auth': {'enable': use_auth}
        }

        self.blocksize = blocksize
        self.target = target
        self.poll_interval = poll_interval

        self.osbs = get_openshift_session(self.workflow, self.openshift_fallback)
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
                    # update image ID with pulp_pull results;
                    # necessary when using Pulp < 2.14. Only do this
                    # when building for a single architecture -- if
                    # building for many, we know Pulp has schema 2
                    # support.
                    if len(worker_metadatas) == 1 and has_pulp_pull:
                        if self.workflow.builder.image_id is not None:
                            instance['extra']['docker']['id'] = self.workflow.builder.image_id

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

        # Append media_types from pulp pull or verify images
        media_results = (self.workflow.exit_results.get(PLUGIN_PULP_PULL_KEY) or
                         self.workflow.exit_results.get(PLUGIN_VERIFY_MEDIA_KEY))
        if media_results:
            media_types += media_results

        if media_types:
            extra['image']['media_types'] = sorted(list(set(media_types)))

    def set_go_metadata(self, extra):
        go = self.workflow.source.config.go
        if go:
            self.log.debug("Setting Go metadata: %s", go)
            extra['image']['go'] = go

    def set_operators_metadata(self, extra, worker_metadatas):
        for platform, metadata in worker_metadatas.items():
            for output in metadata['output']:
                if output.get('filename') == OPERATOR_MANIFESTS_ARCHIVE:
                    extra['operator_manifests_archive'] = OPERATOR_MANIFESTS_ARCHIVE
                    break

    def remove_unavailable_manifest_digests(self, worker_metadatas):
        try:
            available = get_manifests_in_pulp_repository(self.workflow)
        except KeyError:
            # pulp_sync didn't run
            return

        for platform, metadata in worker_metadatas.items():
            for output in metadata['output']:
                if output['type'] != 'docker-image':
                    continue

                unavailable = []
                repositories = output['extra']['docker']['repositories']
                for pullspec in repositories:
                    # Ignore by-tag pullspecs
                    if '@' not in pullspec:
                        continue

                    _, digest = pullspec.split('@', 1)
                    if digest not in available:
                        self.log.info("%s: %s not available, removing", platform, pullspec)
                        unavailable.append(pullspec)

                # Update the list in-place
                for pullspec in unavailable:
                    repositories.remove(pullspec)

    def set_group_manifest_info(self, extra, worker_metadatas):
        version_release = None
        primary_images = get_primary_images(self.workflow)
        for image in primary_images:
            if '-' in image.tag:  # {version}-{release} only, and only one instance
                version_release = image.tag
                break

        if is_scratch_build():
            tags = [image.tag for image in self.workflow.tag_conf.images]
            version_release = tags[0]
        else:
            assert version_release is not None, 'Unable to find version-release image'
            tags = [image.tag for image in primary_images]

        manifest_list_digests = self.workflow.postbuild_results.get(PLUGIN_GROUP_MANIFESTS_KEY)
        if manifest_list_digests:
            index = {}
            index['tags'] = tags
            repositories = self.workflow.build_result.annotations['repositories']['unique']
            repo = ImageName.parse(repositories[0]).to_str(registry=False, tag=False)
            # group_manifests added the registry, so this should be valid
            registries = self.workflow.push_conf.pulp_registries
            if not registries:
                registries = self.workflow.push_conf.all_registries
            for registry in registries:
                manifest_list_digest = manifest_list_digests[repo]
                pullspec = "{0}/{1}@{2}".format(registry.uri, repo, manifest_list_digest.default)
                index['pull'] = [pullspec]
                pullspec = "{0}/{1}:{2}".format(registry.uri, repo,
                                                version_release)
                index['pull'].append(pullspec)

                # Store each digest with according media type
                index['digests'] = {}
                for version, digest in manifest_list_digest.items():
                    if digest:
                        media_type = get_manifest_media_type(version)
                        index['digests'][media_type] = digest
                break
            extra['image']['index'] = index
        # group_manifests returns None if didn't run, {} if group=False
        else:
            for platform in worker_metadatas:
                if platform == "x86_64":
                    for instance in worker_metadatas[platform]['output']:
                        if instance['type'] == 'docker-image':
                            # koji_upload, running in the worker, doesn't have the full tags
                            # so set them here
                            instance['extra']['docker']['tags'] = tags
                            repositories = []
                            for pullspec in instance['extra']['docker']['repositories']:
                                if '@' not in pullspec:
                                    image = ImageName.parse(pullspec)
                                    image.tag = version_release
                                    pullspec = image.to_str()

                                repositories.append(pullspec)

                            instance['extra']['docker']['repositories'] = repositories
                            self.log.debug("reset tags to so that docker is %s",
                                           instance['extra']['docker'])
                            annotations = get_worker_build_info(self.workflow, platform).\
                                build.get_annotations()
                            digests = {}
                            if 'digests' in annotations:
                                digests = get_digests_map_from_annotations(annotations['digests'])
                                instance['extra']['docker']['digests'] = digests

    def get_build(self, metadata, worker_metadatas):
        start_time = int(atomic_reactor_start_time)

        labels = Labels(df_parser(self.workflow.builder.df_path, workflow=self.workflow).labels)
        _, component = labels.get_name_and_value(Labels.LABEL_TYPE_COMPONENT)
        _, version = labels.get_name_and_value(Labels.LABEL_TYPE_VERSION)
        _, release = labels.get_name_and_value(Labels.LABEL_TYPE_RELEASE)

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

        try:
            isolated = str(metadata['labels']['isolated']).lower() == 'true'
        except (IndexError, AttributeError, KeyError):
            isolated = False
        self.log.info("build is isolated: %r", isolated)
        extra['image']['isolated'] = isolated

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

        extra['image'].update(get_parent_image_koji_data(self.workflow))

        flatpak_source_info = get_flatpak_source_info(self.workflow)
        if flatpak_source_info is not None:
            compose_info = get_compose_info(self.workflow)
            koji_metadata = compose_info.koji_metadata()
            koji_metadata['flatpak'] = True
            extra['image'].update(koji_metadata)

        koji_task_owner = get_koji_task_owner(self.session, koji_task_id).get('name')
        extra['submitter'] = self.session.getLoggedInUser()['name']

        resolve_comp_result = self.workflow.prebuild_results.get(PLUGIN_RESOLVE_COMPOSES_KEY)
        if resolve_comp_result:
            extra['image']['odcs'] = {
                'compose_ids': [item['id'] for item in resolve_comp_result['composes']],
                'signing_intent': resolve_comp_result['signing_intent'],
                'signing_intent_overridden': resolve_comp_result['signing_intent_overridden'],
            }
        if self.workflow.all_yum_repourls:
            extra['image']['yum_repourls'] = self.workflow.all_yum_repourls

        self.set_help(extra, worker_metadatas)
        self.set_media_types(extra, worker_metadatas)
        self.set_go_metadata(extra)
        self.set_operators_metadata(extra, worker_metadatas)
        self.remove_unavailable_manifest_digests(worker_metadatas)
        self.set_group_manifest_info(extra, worker_metadatas)

        build = {
            'name': component,
            'version': version,
            'release': release,
            'source': "{0}#{1}".format(source.uri, source.commit_id),
            'start_time': start_time,
            'end_time': int(time.time()),
            'extra': extra,
            'owner': koji_task_owner,
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
        osbs_logs = OSBSLogs(self.log)
        output_files = [add_log_type(add_buildroot_id(md, buildroot_id))
                        for md in osbs_logs.get_log_files(self.osbs, self.build_id)]
        output.extend([of.metadata for of in output_files])

        koji_metadata = {
            'metadata_version': metadata_version,
            'build': build,
            'buildroots': buildroot,
            'output': output,
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

    def upload_scratch_metadata(self, koji_metadata, koji_upload_dir, koji_session):
        metadata_file = NamedTemporaryFile(prefix="metadata", suffix=".json", mode='wb')
        metadata_file.write(json.dumps(koji_metadata, indent=2).encode('utf-8'))
        metadata_file.flush()

        filename = "metadata.json"
        meta_output = Output(file=metadata_file, metadata={'filename': filename})

        try:
            self.upload_file(koji_session, meta_output, koji_upload_dir)
            path = os.path.join(koji_upload_dir, filename)
            log = logging.LoggerAdapter(self.log, {'arch': METADATA_TAG})
            log.info(path)
        finally:
            meta_output.file.close()

    def run(self):
        """
        Run the plugin.
        """
        # Only run if the build was successful
        if self.workflow.build_process_failed:
            self.log.info("Not importing failed build to koji")
            return

        self.session = get_koji_session(self.workflow, self.koji_fallback)

        server_dir = get_koji_upload_dir(self.workflow)

        koji_metadata, output_files = self.combine_metadata_fragments()

        if is_scratch_build():
            self.upload_scratch_metadata(koji_metadata, server_dir, self.session)
            return

        try:
            for output in output_files:
                if output.file:
                    self.upload_file(self.session, output, server_dir)
        finally:
            for output in output_files:
                if output.file:
                    output.file.close()

        try:
            build_info = self.session.CGImport(koji_metadata, server_dir)
        except Exception:
            self.log.debug("metadata: %r", koji_metadata)
            raise

        # Older versions of CGImport do not return a value.
        build_id = build_info.get("id") if build_info else None

        self.log.debug("Build information: %s",
                       json.dumps(build_info, sort_keys=True, indent=4))

        return build_id
