"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import copy
import json
import logging
import os
import tempfile
import time

import koji
import koji_cli.lib

from atomic_reactor import __version__ as atomic_reactor_version
from atomic_reactor.constants import (DEFAULT_DOWNLOAD_BLOCK_SIZE, PROG,
                                      KOJI_BTYPE_OPERATOR_MANIFESTS,
                                      OPERATOR_MANIFESTS_ARCHIVE,
                                      PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY,
                                      PLUGIN_RESOLVE_REMOTE_SOURCE,
                                      REMOTE_SOURCES_FILENAME, KOJI_MAX_RETRIES,
                                      KOJI_RETRY_INTERVAL, KOJI_OFFLINE_RETRY_INTERVAL)
from atomic_reactor.util import (get_version_of_tools, get_docker_architecture,
                                 Output, get_image_upload_filename,
                                 get_checksums, get_manifest_media_type)
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin
from atomic_reactor.utils.rpm import get_rpm_list, parse_rpm_output

logger = logging.getLogger(__name__)


class KojiUploadLogger(object):
    def __init__(self, logger, notable_percent=10):
        self.logger = logger
        self.notable_percent = notable_percent
        self.last_percent_done = 0

    def callback(self, offset, totalsize, size, t1, t2):  # pylint: disable=W0613
        if offset == 0:
            self.logger.debug("upload size: %.1fMiB", totalsize / 1024 / 1024)

        if not totalsize or not t1:
            return

        percent_done = 100 * offset // totalsize
        if (percent_done >= 99 or
                percent_done - self.last_percent_done >= self.notable_percent):
            self.last_percent_done = percent_done
            self.logger.debug("upload: %d%% done (%.1f MiB/sec)",
                              percent_done, size / t1 / 1024 / 1024)


def koji_login(session,
               proxyuser=None,
               ssl_certs_dir=None,
               krb_principal=None,
               krb_keytab=None):
    """
    Choose the correct login method based on the available credentials,
    and call that method on the provided session object.

    :param session: koji.ClientSession instance
    :param proxyuser: str, proxy user
    :param ssl_certs_dir: str, path to "cert" (required), and "serverca" (optional)
    :param krb_principal: str, name of Kerberos principal
    :param krb_keytab: str, Kerberos keytab
    :return: None
    """

    kwargs = {}
    if proxyuser:
        kwargs['proxyuser'] = proxyuser

    if ssl_certs_dir:
        # Use certificates
        logger.info("Using SSL certificates for Koji authentication")
        kwargs['cert'] = os.path.join(ssl_certs_dir, 'cert')

        # serverca is not required in newer versions of koji, but if set
        # koji will always ensure file exists
        # NOTE: older versions of koji may require this to be set, in
        # that case, make sure serverca is passed in
        serverca_path = os.path.join(ssl_certs_dir, 'serverca')
        if os.path.exists(serverca_path):
            kwargs['serverca'] = serverca_path

        # Older versions of koji actually require this parameter, even though
        # it's completely ignored.
        kwargs['ca'] = None

        result = session.ssl_login(**kwargs)
    else:
        # Use Kerberos
        logger.info("Using Kerberos for Koji authentication")
        if krb_principal and krb_keytab:
            kwargs['principal'] = krb_principal
            kwargs['keytab'] = krb_keytab

        result = session.krb_login(**kwargs)

    if not result:
        raise RuntimeError('Unable to perform Koji authentication')

    return result


def create_koji_session(hub_url, auth_info=None, use_fast_upload=True):
    """
    Creates and returns a Koji session. If auth_info
    is provided, the session will be authenticated.

    :param hub_url: str, Koji hub URL
    :param auth_info: dict, authentication parameters used for koji_login
    :param use_fast_upload: bool, flag to use or not Koji's fast upload API.
    :return: koji.ClientSession instance
    """
    session = koji.ClientSession(hub_url,
                                 opts={'krb_rdns': False,
                                       'use_fast_upload': use_fast_upload,
                                       'anon_retry': True,
                                       'max_retries': KOJI_MAX_RETRIES,
                                       'retry_interval': KOJI_RETRY_INTERVAL,
                                       'offline_retry': True,
                                       'offline_retry_interval': KOJI_OFFLINE_RETRY_INTERVAL})

    if auth_info is not None:
        koji_login(session, **auth_info)

    return session


class TaskWatcher(object):
    def __init__(self, session, task_id, poll_interval=5):
        self.session = session
        self.task_id = task_id
        self.poll_interval = poll_interval
        self.state = 'CANCELED'

    def wait(self):
        logger.debug("waiting for koji task %r to finish", self.task_id)
        while not self.session.taskFinished(self.task_id):
            time.sleep(self.poll_interval)

        logger.debug("koji task is finished, getting info")
        task_info = self.session.getTaskInfo(self.task_id, request=True)
        self.state = koji.TASK_STATES[task_info['state']]
        return self.state

    def failed(self):
        return self.state in ['CANCELED', 'FAILED']


def stream_task_output(session, task_id, file_name,
                       blocksize=DEFAULT_DOWNLOAD_BLOCK_SIZE):
    """
    Generator to download file from task without loading the whole
    file into memory.
    """
    logger.debug('Streaming %s from task %s', file_name, task_id)
    offset = 0
    contents = '[PLACEHOLDER]'
    while contents:
        contents = session.downloadTaskOutput(task_id, file_name, offset,
                                              blocksize)
        offset += len(contents)
        if contents:
            yield contents

    logger.debug('Finished streaming %s from task %s', file_name, task_id)


def tag_koji_build(session, build_id, target, poll_interval=5):
    logger.debug('Finding build tag for target %s', target)
    target_info = session.getBuildTarget(target)
    build_tag = target_info['dest_tag_name']
    logger.info('Tagging build with %s', build_tag)
    task_id = session.tagBuild(build_tag, build_id)

    task = TaskWatcher(session, task_id, poll_interval=poll_interval)
    task.wait()
    if task.failed():
        raise RuntimeError('Task %s failed to tag koji build' % task_id)

    return build_tag


def get_koji_task_owner(session, task_id, default=None):
    default = {} if default is None else default
    if task_id:
        try:
            koji_task_info = session.getTaskInfo(task_id)
            koji_task_owner = session.getUser(koji_task_info['owner'])
        except Exception:
            logger.exception('Unable to get Koji task owner')
            koji_task_owner = default
    else:
        koji_task_owner = default
    return koji_task_owner


def get_koji_module_build(session, module_spec):
    """
    Get build information from Koji for a module. The module specification must
    include at least name, stream and version. For legacy support, you can omit
    context if there is only one build of the specified NAME:STREAM:VERSION.

    :param session: koji.ClientSession, Session for talking to Koji
    :param module_spec: ModuleSpec, specification of the module version
    :return: tuple, a dictionary of information about the build, and
        a list of RPMs in the module build
    """

    if module_spec.context is not None:
        # The easy case - we can build the koji "name-version-release" out of the
        # module spec.
        koji_nvr = "{}-{}-{}.{}".format(module_spec.name,
                                        module_spec.stream.replace("-", "_"),
                                        module_spec.version,
                                        module_spec.context)
        logger.info("Looking up module build %s in Koji", koji_nvr)
        build = session.getBuild(koji_nvr)
    else:
        # Without the context, we have to retrieve all builds for the module, and
        # find the one we want. This is pretty inefficient, but won't be needed
        # long-term.
        logger.info("Listing all builds for %s in Koji", module_spec.name)
        package_id = session.getPackageID(module_spec.name)
        builds = session.listBuilds(packageID=package_id, type='module',
                                    state=koji.BUILD_STATES['COMPLETE'])
        build = None

        for b in builds:
            if '.' in b['release']:
                version, _ = b['release'].split('.', 1)
                if version == module_spec.version:
                    if build is not None:
                        raise RuntimeError("Multiple builds found for {}"
                                           .format(module_spec.to_str()))
                    else:
                        build = b

    if build is None:
        raise RuntimeError("No build found for {}".format(module_spec.to_str()))

    archives = session.listArchives(buildID=build['build_id'])
    # The RPM list for the 'modulemd.txt' archive has all the RPMs, recent
    # versions of MBS also write upload 'modulemd.<arch>.txt' archives with
    # architecture subsets.
    archives = [a for a in archives if a['filename'] == 'modulemd.txt']
    assert len(archives) == 1

    rpm_list = session.listRPMs(imageID=archives[0]['id'])

    return build, rpm_list


def get_buildroot(build_id, tasker, osbs, rpms):
    """
    Build the buildroot entry of the metadata.
    :param build_id: str, ocp build_id
    :param tasker: ContainerTasker instance
    :param osbs: OSBS instance
    :param rpms: bool, get rpms for components metadata
    :return: dict, partial metadata
    """
    docker_info = tasker.get_info()
    host_arch, docker_version = get_docker_architecture(tasker)

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
        'extra': {
            'osbs': {
                'build_id': build_id,
                'builder_image_id': get_builder_image_id(build_id, osbs),
            }
        },
        'components': [],
    }
    if rpms:
        buildroot['components'] = get_rpms()

    return buildroot


def get_image_output(workflow, image_id, arch):
    """
    Create the output for the image

    This is the Koji Content Generator metadata, along with the
    'docker save' output to upload.

    :return: tuple, (metadata dict, Output instance)

    """
    saved_image = workflow.exported_image_sequence[-1].get('path')
    image_name = get_image_upload_filename(workflow.exported_image_sequence[-1],
                                           image_id, arch)

    metadata = get_output_metadata(saved_image, image_name)
    output = Output(file=open(saved_image), metadata=metadata)

    return metadata, output


def get_source_tarball_output(workflow):
    plugin_results = workflow.prebuild_results.get(PLUGIN_RESOLVE_REMOTE_SOURCE) or {}
    remote_source_path = plugin_results.get('remote_source_path')
    if not remote_source_path:
        return None

    metadata = get_output_metadata(remote_source_path, REMOTE_SOURCES_FILENAME)
    output = Output(file=open(remote_source_path), metadata=metadata)
    return output


def get_remote_source_json_output(workflow):
    plugin_results = workflow.prebuild_results.get(PLUGIN_RESOLVE_REMOTE_SOURCE) or {}
    remote_source_json = plugin_results.get('remote_source_json')
    if not remote_source_json:
        return None

    remote_source_json_filename = 'remote-source.json'
    tmpdir = tempfile.mkdtemp()
    file_path = os.path.join(tmpdir, remote_source_json_filename)
    with open(file_path, 'w') as f:
        json.dump(remote_source_json, f, indent=4, sort_keys=True)

    metadata = get_output_metadata(file_path, remote_source_json_filename)
    output = Output(file=open(file_path), metadata=metadata)
    return output


def get_image_components(workflow):
    """
    Re-package the output of the rpmqa plugin into the format required
    for the metadata.
    """
    output = workflow.image_components
    if output is None:
        logger.error("%s plugin did not run!", PostBuildRPMqaPlugin.key)
        output = []

    return output


def add_custom_type(output, custom_type, content=None):
    output.metadata.update({
        'type': custom_type,
        'extra': {
            'typeinfo': {
                custom_type: content or {}
            },
        },
    })


def get_output(workflow, buildroot_id, pullspec, platform, source_build=False, logs=None):
    """
    Build the 'output' section of the metadata.
    :param buildroot_id: str, buildroot_id
    :param pullspec: ImageName
    :param platform: str, output platform
    :param source_build: bool, is source_build ?
    :param logs: list, of Output logs
    :return: tuple, list of Output instances, and extra Output file
    """
    def add_buildroot_id(output):
        logfile, metadata = output
        metadata.update({'buildroot_id': buildroot_id})
        return Output(file=logfile, metadata=metadata)

    def add_log_type(output, arch):
        logfile, metadata = output
        metadata.update({'type': 'log', 'arch': arch})
        return Output(file=logfile, metadata=metadata)

    extra_output_file = None
    output_files = []

    arch = os.uname()[4]

    if source_build:
        image_id = workflow.koji_source_manifest['config']['digest']
        # we are using digest from manifest, because we can't get diff_ids
        # unless we pull image, which would fail due because there are so many layers
        layer_sizes = [{'digest': layer['digest'], 'size': layer['size']}
                       for layer in workflow.koji_source_manifest['layers']]
        platform = arch

    else:
        output_files = [add_log_type(add_buildroot_id(metadata), arch)
                        for metadata in logs]

        # Parent of squashed built image is base image
        image_id = workflow.builder.image_id
        parent_id = None
        if not workflow.builder.dockerfile_images.base_from_scratch:
            parent_id = workflow.builder.base_image_inspect['Id']

        layer_sizes = workflow.layer_sizes

    registries = workflow.push_conf.docker_registries
    config = copy.deepcopy(registries[0].config)

    # We don't need container_config section
    if config and 'container_config' in config:
        del config['container_config']

    repositories, typed_digests = get_repositories_and_digests(workflow, pullspec)

    tags = set(image.tag for image in workflow.tag_conf.images)
    metadata, output = get_image_output(workflow, image_id, platform)

    metadata.update({
        'arch': arch,
        'type': 'docker-image',
        'components': [],
        'extra': {
            'image': {
                'arch': arch,
            },
            'docker': {
                'id': image_id,
                'repositories': repositories,
                'layer_sizes': layer_sizes,
                'tags': list(tags),
                'config': config,
                'digests': typed_digests,
            },
        },
    })

    if not config:
        del metadata['extra']['docker']['config']

    if not source_build:
        metadata['components'] = get_image_components(workflow)

        if not workflow.builder.dockerfile_images.base_from_scratch:
            metadata['extra']['docker']['parent_id'] = parent_id

    # Add the 'docker save' image to the output
    image = add_buildroot_id(output)

    # when doing regular build, worker already uploads image,
    # so orchestrator needs only metadata,
    # but source contaiener build didn't upload that image yet,
    # so we want metadata, and the image to upload
    if source_build:
        output_files.append(metadata)
        extra_output_file = output
    else:
        output_files.append(image)

    if not source_build:
        # add operator manifests to output
        operator_manifests_path = (workflow.postbuild_results
                                   .get(PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY))
        if operator_manifests_path:
            operator_manifests_file = open(operator_manifests_path)
            manifests_metadata = get_output_metadata(operator_manifests_path,
                                                     OPERATOR_MANIFESTS_ARCHIVE)
            operator_manifests_output = Output(file=operator_manifests_file,
                                               metadata=manifests_metadata)
            add_custom_type(operator_manifests_output, KOJI_BTYPE_OPERATOR_MANIFESTS)

            operator_manifests = add_buildroot_id(operator_manifests_output)
            output_files.append(operator_manifests)

    return output_files, extra_output_file


def generate_koji_upload_dir():
    """
    Create a path name for uploading files to

    :return: str, path name expected to be unique
    """
    return koji_cli.lib.unique_path('koji-upload')


def get_output_metadata(path, filename):
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


def get_rpms():
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

    output = get_rpm_list(tags)

    return parse_rpm_output(output, tags)


def get_builder_image_id(build_id, osbs):
    """
    Retrieve the docker ID of the buildroot image we are in from the environment.
    """
    return os.environ.get('OPENSHIFT_CUSTOM_BUILD_BASE_IMAGE', '')


def select_digest(digests):
    digest = digests.default

    return digest


def get_repositories_and_digests(workflow, pullspec_image):
    """
    Returns a map of images to their repositories and a map of media types to each digest

    it creates a map of images to digests, which is need to create the image->repository
    map and uses the same loop structure as media_types->digest, but the image->digest
    map isn't needed after we have the image->repository map and can be discarded.
    """
    digests = {}  # image -> digests
    typed_digests = {}  # media_type -> digests
    for registry in workflow.push_conf.docker_registries:
        for image in workflow.tag_conf.images:
            image_str = image.to_str()
            if image_str in registry.digests:
                image_digests = registry.digests[image_str]
                digest_list = [select_digest(image_digests)]
                digests[image.to_str(registry=False)] = digest_list
                for digest_version in image_digests.content_type:
                    if digest_version == 'v1':
                        continue
                    if digest_version not in image_digests:
                        continue
                    digest_type = get_manifest_media_type(digest_version)
                    typed_digests[digest_type] = image_digests[digest_version]

    registries = workflow.push_conf.all_registries
    repositories = []
    for registry in registries:
        image = pullspec_image.copy()
        image.registry = registry.uri
        pullspec = image.to_str()

        repositories.append(pullspec)

        digest_list = digests.get(image.to_str(registry=False), ())
        for digest in digest_list:
            digest_pullspec = image.to_str(tag=False) + "@" + digest
            repositories.append(digest_pullspec)

    return repositories, typed_digests
