"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import copy
import json
import koji
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
from atomic_reactor.util import (OSBSLogs, get_parent_image_koji_data, get_manifest_media_version,
                                 is_manifest_list)
from atomic_reactor.utils.koji import (
        get_buildroot, get_output, generate_koji_upload_dir, add_custom_type,
        get_source_tarball_output, get_remote_source_json_output
)
from atomic_reactor.plugins.pre_reactor_config import get_openshift_session
from atomic_reactor.plugins.pre_fetch_sources import PLUGIN_FETCH_SOURCES_KEY

try:
    from atomic_reactor.plugins.pre_flatpak_update_dockerfile import get_flatpak_compose_info
except ImportError:
    # modulemd not available
    def get_flatpak_compose_info(_):
        return None

from atomic_reactor.constants import (
    PROG,
    PLUGIN_KOJI_IMPORT_PLUGIN_KEY,
    PLUGIN_FETCH_WORKER_METADATA_KEY, PLUGIN_GROUP_MANIFESTS_KEY, PLUGIN_RESOLVE_COMPOSES_KEY,
    PLUGIN_VERIFY_MEDIA_KEY,
    PLUGIN_PUSH_OPERATOR_MANIFESTS_KEY,
    PLUGIN_RESOLVE_REMOTE_SOURCE,
    METADATA_TAG, OPERATOR_MANIFESTS_ARCHIVE,
    KOJI_BTYPE_REMOTE_SOURCES,
    KOJI_BTYPE_OPERATOR_MANIFESTS,
    KOJI_KIND_IMAGE_BUILD,
    KOJI_KIND_IMAGE_SOURCE_BUILD,
    KOJI_SUBTYPE_OP_APPREGISTRY,
    KOJI_SUBTYPE_OP_BUNDLE,
    KOJI_SOURCE_ENGINE,
)
from atomic_reactor.util import (Output, get_build_json,
                                 df_parser, get_primary_images,
                                 get_floating_images, get_unique_images,
                                 get_manifest_media_type,
                                 get_digests_map_from_annotations, is_scratch_build,
                                 has_operator_bundle_manifest,
                                 has_operator_appregistry_manifest,
                                 )
from atomic_reactor.utils.koji import (KojiUploadLogger, get_koji_task_owner)
from atomic_reactor.plugins.pre_reactor_config import get_koji_session, get_koji
from atomic_reactor.metadata import label
from osbs.utils import Labels, ImageName


@label('koji-build-id')
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

        :param tasker: ContainerTasker instance
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
        self.session = None
        self.reserve_build = get_koji(self.workflow, self.koji_fallback).get('reserve_build', False)
        self.source_build = bool(self.workflow.build_result.oci_image_path)

    def get_output(self, worker_metadatas, buildroot_id):
        """
        Build the output entry of the metadata.

        :return: list, containing dicts of partial metadata
        """
        outputs = []
        output_file = None

        if self.source_build:

            registry = self.workflow.push_conf.docker_registries[0]

            build_name = get_unique_images(self.workflow)[0]
            pullspec = copy.deepcopy(build_name)
            pullspec.registry = registry.uri

            outputs, output_file = get_output(workflow=self.workflow, buildroot_id=buildroot_id,
                                              pullspec=pullspec, platform=os.uname()[4],
                                              source_build=True, logs=None)

        else:
            for platform in worker_metadatas:
                for instance in worker_metadatas[platform]['output']:
                    instance['buildroot_id'] = '{}-{}'.format(platform, instance['buildroot_id'])
                    outputs.append(instance)

        return outputs, output_file

    def get_buildroot(self, worker_metadatas):
        """
        Build the buildroot entry of the metadata.

        :return: list, containing dicts of partial metadata
        """
        buildroots = []

        if self.source_build:
            buildroot = get_buildroot(build_id=self.build_id, tasker=self.tasker,
                                      osbs=self.osbs, rpms=False)
            buildroot['id'] = '{}-{}'.format(buildroot['container']['arch'], buildroot['id'])

            registry = self.workflow.push_conf.docker_registries[0]
            build_name = get_unique_images(self.workflow)[0].to_str()

            manifest_digest = registry.digests[build_name]
            digest_version = get_manifest_media_version(manifest_digest)
            media_type = get_manifest_media_type(digest_version)

            buildroot['extra']['osbs']['koji'] = {
                'build_name': build_name,
                'builder_image_id': {media_type: manifest_digest.default}
            }

            buildroots.append(buildroot)
        else:
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
        if not self.source_build:
            for platform in worker_metadatas:
                annotations = get_worker_build_info(self.workflow,
                                                    platform).build.get_annotations()
                if annotations.get('media-types'):
                    media_types = json.loads(annotations['media-types'])
                    break

        # Append media_types from verify images
        media_results = self.workflow.exit_results.get(PLUGIN_VERIFY_MEDIA_KEY)
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
        # update push plugin and uploaded manifests file independently as push plugin may fail
        op_push_res = self.workflow.postbuild_results.get(PLUGIN_PUSH_OPERATOR_MANIFESTS_KEY)
        if op_push_res:
            extra.update({
                "operator_manifests": {
                    "appregistry": op_push_res
                }
            })

        for metadata in worker_metadatas.values():
            for output in metadata['output']:
                if output.get('filename') == OPERATOR_MANIFESTS_ARCHIVE:
                    extra['operator_manifests_archive'] = OPERATOR_MANIFESTS_ARCHIVE
                    operators_typeinfo = {
                        KOJI_BTYPE_OPERATOR_MANIFESTS: {
                            'archive': OPERATOR_MANIFESTS_ARCHIVE,
                        },
                    }
                    extra.setdefault('typeinfo', {}).update(operators_typeinfo)

                    return  # only one worker can process operator manifests

    def set_remote_sources_metadata(self, extra):
        remote_source_result = self.workflow.prebuild_results.get(PLUGIN_RESOLVE_REMOTE_SOURCE)
        if remote_source_result:
            url = remote_source_result['annotations']['remote_source_url']
            remote_source_typeinfo = {
                KOJI_BTYPE_REMOTE_SOURCES: {
                    'remote_source_url': url,
                },
            }
            extra.setdefault('typeinfo', {}).update(remote_source_typeinfo)

            # TODO: is setting it in the image metadata also needed?
            extra['image']['remote_source_url'] = url

    def set_group_manifest_info(self, extra, worker_metadatas):
        version_release = None
        primary_images = get_primary_images(self.workflow)
        floating_images = get_floating_images(self.workflow)
        unique_images = get_unique_images(self.workflow)
        if primary_images:
            version_release = primary_images[0].tag

        if is_scratch_build(self.workflow):
            tags = [image.tag for image in self.workflow.tag_conf.images]
            version_release = tags[0]
        else:
            assert version_release is not None, 'Unable to find version-release image'
            tags = [image.tag for image in primary_images]

        floating_tags = [image.tag for image in floating_images]
        unique_tags = [image.tag for image in unique_images]

        manifest_data = self.workflow.postbuild_results.get(PLUGIN_GROUP_MANIFESTS_KEY, {})
        if manifest_data and is_manifest_list(manifest_data.get("media_type")):
            manifest_digest = manifest_data.get("manifest_digest")
            index = {}
            index['tags'] = tags
            index['floating_tags'] = floating_tags
            index['unique_tags'] = unique_tags
            repositories = self.workflow.build_result.annotations['repositories']['unique']
            repo = ImageName.parse(repositories[0]).to_str(registry=False, tag=False)
            # group_manifests added the registry, so this should be valid
            registries = self.workflow.push_conf.all_registries

            digest_version = get_manifest_media_version(manifest_digest)
            digest = manifest_digest.default

            for registry in registries:
                pullspec = "{0}/{1}@{2}".format(registry.uri, repo, digest)
                index['pull'] = [pullspec]
                pullspec = "{0}/{1}:{2}".format(registry.uri, repo,
                                                version_release)
                index['pull'].append(pullspec)

                # Store each digest with according media type
                index['digests'] = {}
                media_type = get_manifest_media_type(digest_version)
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
                            instance['extra']['docker']['floating_tags'] = floating_tags
                            instance['extra']['docker']['unique_tags'] = unique_tags
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
        extra = {'image': {}, 'osbs_build': {'subtypes': []}}

        if not self.source_build:
            labels = Labels(df_parser(self.workflow.builder.df_path,
                                      workflow=self.workflow).labels)
            _, component = labels.get_name_and_value(Labels.LABEL_TYPE_COMPONENT)
            _, version = labels.get_name_and_value(Labels.LABEL_TYPE_VERSION)
            _, release = labels.get_name_and_value(Labels.LABEL_TYPE_RELEASE)

            source = self.workflow.source
            if not isinstance(source, GitSource):
                raise RuntimeError('git source required')

            extra['image']['autorebuild'] = is_rebuild(self.workflow)
            if self.workflow.triggered_after_koji_task:
                extra['image']['triggered_after_koji_task'] =\
                    self.workflow.triggered_after_koji_task

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

            flatpak_compose_info = get_flatpak_compose_info(self.workflow)
            if flatpak_compose_info:
                koji_metadata = flatpak_compose_info.koji_metadata()
                koji_metadata['flatpak'] = True
                extra['image'].update(koji_metadata)
                extra['osbs_build']['subtypes'].append('flatpak')

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
            self.set_operators_metadata(extra, worker_metadatas)
            self.set_remote_sources_metadata(extra)

            self.set_go_metadata(extra)
            self.set_group_manifest_info(extra, worker_metadatas)
            extra['osbs_build']['kind'] = KOJI_KIND_IMAGE_BUILD
            extra['osbs_build']['engine'] = self.workflow.builder.tasker.build_method
            if has_operator_appregistry_manifest(self.workflow):
                extra['osbs_build']['subtypes'].append(KOJI_SUBTYPE_OP_APPREGISTRY)
            if has_operator_bundle_manifest(self.workflow):
                extra['osbs_build']['subtypes'].append(KOJI_SUBTYPE_OP_BUNDLE)
        else:
            source_result = self.workflow.prebuild_results[PLUGIN_FETCH_SOURCES_KEY]
            extra['image']['sources_for_nvr'] = source_result['sources_for_nvr']
            extra['image']['sources_signing_intent'] = source_result['signing_intent']
            extra['osbs_build']['kind'] = KOJI_KIND_IMAGE_SOURCE_BUILD
            extra['osbs_build']['engine'] = KOJI_SOURCE_ENGINE

        koji_task_id = metadata.get('labels', {}).get('koji-task-id')
        if koji_task_id is not None:
            self.log.info("build configuration created by Koji Task ID %s",
                          koji_task_id)
            try:
                extra['container_koji_task_id'] = int(koji_task_id)
            except ValueError:
                self.log.error("invalid task ID %r", koji_task_id, exc_info=1)

        koji_task_owner = get_koji_task_owner(self.session, koji_task_id).get('name')
        extra['submitter'] = self.session.getLoggedInUser()['name']

        self.set_media_types(extra, worker_metadatas)

        build = {
            'start_time': start_time,
            'end_time': int(time.time()),
            'extra': extra,
            'owner': koji_task_owner,
        }
        if self.source_build:
            build.update({
                'name': self.workflow.koji_source_nvr['name'],
                'version': self.workflow.koji_source_nvr['version'],
                'release': self.workflow.koji_source_nvr['release'],
                'source': self.workflow.koji_source_source_url,
            })
        else:
            build.update({
                'name': component,
                'version': version,
                'release': release,
                'source': "{0}#{1}".format(source.uri, source.commit_id),
            })

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
        output, output_file = self.get_output(worker_metadatas, buildroot_id)
        osbs_logs = OSBSLogs(self.log)
        output_files = [add_log_type(add_buildroot_id(md, buildroot_id))
                        for md in osbs_logs.get_log_files(self.osbs, self.build_id)]

        output.extend([of.metadata for of in output_files])
        if output_file:
            output_files.append(output_file)

        # add remote source tarball and remote-source.json files to output
        for remote_source_output in [
            get_source_tarball_output(self.workflow),
            get_remote_source_json_output(self.workflow)
        ]:
            if remote_source_output:
                add_custom_type(remote_source_output, KOJI_BTYPE_REMOTE_SOURCES)
                remote_source = add_buildroot_id(remote_source_output, buildroot_id)
                output_files.append(remote_source)
                output.append(remote_source.metadata)

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

        # get the session and token information in case we need to refund a failed build
        self.session = get_koji_session(self.workflow, self.koji_fallback)
        build_token = self.workflow.reserved_token
        build_id = self.workflow.reserved_build_id

        # Only run if the build was successful
        if self.workflow.build_process_failed:
            self.log.info("Not importing %s build to koji",
                          "canceled" if self.workflow.build_canceled else "failed")
            if self.reserve_build and build_token is not None:
                state = koji.BUILD_STATES['FAILED']
                if self.workflow.build_canceled:
                    state = koji.BUILD_STATES['CANCELED']
                self.session.CGRefundBuild(PROG, build_id, build_token, state)
            return

        if self.source_build:
            server_dir = generate_koji_upload_dir()
        else:
            server_dir = get_koji_upload_dir(self.workflow)

        koji_metadata, output_files = self.combine_metadata_fragments()

        if is_scratch_build(self.workflow):
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

        if build_id is not None and build_token is not None:
            koji_metadata['build']['build_id'] = build_id

        try:
            if build_token:
                build_info = self.session.CGImport(koji_metadata, server_dir, token=build_token)
            else:
                build_info = self.session.CGImport(koji_metadata, server_dir)

        except Exception:
            self.log.debug("metadata: %r", koji_metadata)
            raise

        # Older versions of CGImport do not return a value.
        build_id = build_info.get("id") if build_info else None

        self.log.debug("Build information: %s",
                       json.dumps(build_info, sort_keys=True, indent=4))

        return build_id
