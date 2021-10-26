"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from collections import namedtuple
from copy import deepcopy
from multiprocessing.pool import ThreadPool

import json
from operator import attrgetter
import time
import logging
from datetime import timedelta
import datetime as dt
import copy
import platform

from atomic_reactor.inner import BuildResult
from atomic_reactor.plugin import BuildStepPlugin
from atomic_reactor.util import (df_parser, get_manifest_list,
                                 get_platforms, map_to_user_params)
from atomic_reactor.utils.koji import generate_koji_upload_dir
from atomic_reactor.constants import (PLUGIN_ADD_FILESYSTEM_KEY, PLUGIN_BUILD_ORCHESTRATE_KEY)
from atomic_reactor.config import get_openshift_session
from osbs.api import OSBS
from osbs.exceptions import OsbsException
from osbs.conf import Configuration
from osbs.utils import Labels, ImageName


ClusterInfo = namedtuple('ClusterInfo', ('cluster', 'platform', 'osbs', 'load'))
WORKSPACE_KEY_BUILD_INFO = 'build_info'
WORKSPACE_KEY_UPLOAD_DIR = 'koji_upload_dir'
WORKSPACE_KEY_OVERRIDE_KWARGS = 'override_kwargs'
FIND_CLUSTER_RETRY_DELAY = 15.0
FAILURE_RETRY_DELAY = 10.0
MAX_CLUSTER_FAILS = 20


def get_build_json():
    """
    FOR MOCKING ONLY
    This only exists to allow OSBS2 unit tests to pass
    """
    return {}


def get_worker_build_info(workflow, platform):
    """
    Obtain worker build information for a given platform
    """
    workspace = workflow.plugin_workspace[OrchestrateBuildPlugin.key]
    return workspace[WORKSPACE_KEY_BUILD_INFO][platform]


def get_koji_upload_dir(workflow):
    """
    Obtain koji_upload_dir value used for worker builds
    """
    workspace = workflow.plugin_workspace[OrchestrateBuildPlugin.key]
    return workspace[WORKSPACE_KEY_UPLOAD_DIR]


def override_build_kwarg(workflow, k, v, platform=None):
    """
    Override a build-kwarg for all worker builds
    """
    key = OrchestrateBuildPlugin.key
    # Use None to indicate an override for all platforms

    workspace = workflow.plugin_workspace.setdefault(key, {})
    override_kwargs = workspace.setdefault(WORKSPACE_KEY_OVERRIDE_KWARGS, {})
    override_kwargs.setdefault(platform, {})
    override_kwargs[platform][k] = v


class UnknownPlatformException(Exception):
    """ No clusters could be found for a platform """


class AllClustersFailedException(Exception):
    """ Each cluster has reached max_cluster_fails """


class UnknownKindException(Exception):
    """ Build image from contains unknown kind """


class ClusterRetryContext(object):
    def __init__(self, max_cluster_fails):
        # how many times this cluster has failed
        self.fails = 0

        # datetime at which attempts can resume
        self.retry_at = dt.datetime.utcfromtimestamp(0)

        # the number of fail counts before this cluster is considered dead
        self.max_cluster_fails = max_cluster_fails

    @property
    def failed(self):
        """Is this cluster considered dead?"""
        return self.fails >= self.max_cluster_fails

    @property
    def in_retry_wait(self):
        """Should we wait before trying this cluster again?"""
        return dt.datetime.now() < self.retry_at

    def try_again_later(self, seconds):
        """Put this cluster in retry-wait (or consider it dead)"""
        if not self.failed:
            self.fails += 1
            self.retry_at = (dt.datetime.now() + timedelta(seconds=seconds))


def wait_for_any_cluster(contexts):
    """
    Wait until any of the clusters are out of retry-wait

    :param contexts: List[ClusterRetryContext]
    :raises: AllClustersFailedException if no more retry attempts allowed
    """
    try:
        earliest_retry_at = min(ctx.retry_at for ctx in contexts.values()
                                if not ctx.failed)
    except ValueError as exc:  # can't take min() of empty sequence
        raise AllClustersFailedException(
            "Could not find appropriate cluster for worker build."
        ) from exc

    time_until_next = earliest_retry_at - dt.datetime.now()
    time.sleep(max(timedelta(seconds=0), time_until_next).seconds)


class WorkerBuildInfo(object):

    def __init__(self, build, cluster_info, logger):
        self.build = build
        self.cluster = cluster_info.cluster
        self.osbs = cluster_info.osbs
        self.platform = cluster_info.platform
        self.log = logging.LoggerAdapter(logger, {'arch': self.platform})

        self.monitor_exception = None

    @property
    def name(self):
        return self.build.get_build_name() if self.build else 'N/A'

    def wait_to_finish(self):
        self.build = self.osbs.wait_for_build_to_finish(self.name)
        return self.build

    def watch_logs(self):
        for line in self.osbs.get_build_logs(self.name, follow=True, decode=True):
            self.log.info(line)

    def get_annotations(self):
        build_annotations = self.build.get_annotations() or {}
        annotations = {
            'build': {
                'cluster-url': self.osbs.os_conf.get_openshift_base_uri(),
                'namespace': self.osbs.os_conf.get_namespace(),
                'build-name': self.name,
            },
            'digests': json.loads(
                build_annotations.get('digests', '[]')),
            'plugins-metadata': json.loads(
                build_annotations.get('plugins-metadata', '{}')),
        }

        if 'metadata_fragment' in build_annotations and \
           'metadata_fragment_key' in build_annotations:
            annotations['metadata_fragment'] = build_annotations['metadata_fragment']
            annotations['metadata_fragment_key'] = build_annotations['metadata_fragment_key']

        return annotations

    def get_fail_reason(self):
        fail_reason = {}
        if self.monitor_exception:
            fail_reason['general'] = str(self.monitor_exception)
        elif not self.build:
            fail_reason['general'] = 'build not started'

        if not self.build:
            return fail_reason

        build_annotations = self.build.get_annotations() or {}
        metadata = json.loads(build_annotations.get('plugins-metadata', '{}'))
        if self.monitor_exception:
            fail_reason['general'] = str(self.monitor_exception)

        try:
            fail_reason.update(metadata['errors'])
        except KeyError:
            try:
                build_name = self.build.get_build_name()
                pod = self.osbs.get_pod_for_build(build_name)
                fail_reason['pod'] = pod.get_failure_reason()
            except (OsbsException, AttributeError):
                # Catch AttributeError here because osbs-client < 0.41
                # doesn't have this method
                pass

        return fail_reason

    def cancel_build(self):
        if self.build and not self.build.is_finished():
            self.osbs.cancel_build(self.name)


class OrchestrateBuildPlugin(BuildStepPlugin):
    """
    Start and monitor worker builds for each platform

    This plugin will find the best suited worker cluster to
    be used for each platform. It does so by calculating the
    current load of active builds on each cluster and choosing
    the one with smallest load.

    The list of available worker clusters is retrieved by fetching
    the result provided by reactor_config plugin.

    If any of the worker builds fail, this plugin will return a
    failed BuildResult. Although, it does wait for all worker builds
    to complete in any case.

    If all worker builds succeed, then this plugin returns a
    successful BuildResult, but with a remote image result. The
    image is built in the worker builds which is likely a different
    host than the one running this build. This means that the local
    docker daemon has no knowledge of the built image.

    If build_image is defined it is passed to the worker build,
    but there is still possibility to have build_imagestream inside
    osbs.conf in the secret, and that would take precendence over
    build_image from kwargs
    """

    UNREACHABLE_CLUSTER_LOAD = object()

    key = PLUGIN_BUILD_ORCHESTRATE_KEY

    @staticmethod
    def args_from_user_params(user_params: dict) -> dict:
        args = {}

        if not user_params.get("buildroot_is_imagestream") and "build_image" in user_params:
            args["config_kwargs"] = {"build_from": f"image:{user_params['build_image']}"}

        build_kwargs_from_user_params = map_to_user_params(
            "component",
            "git_branch",
            "git_ref",
            "git_uri",
            "koji_task_id",
            "filesystem_koji_task_id",
            "scratch",
            "target:koji_target",
            "user",
            "yum_repourls",
            "koji_parent_build",
            "isolated",
            "reactor_config_map",
            "reactor_config_override",
            "git_commit_depth",
            "flatpak",
            "operator_csv_modifications_url",
        )
        if build_kwargs := build_kwargs_from_user_params(user_params):
            args["build_kwargs"] = build_kwargs

        return args

    def __init__(self, workflow, build_kwargs=None,
                 worker_build_image=None, config_kwargs=None,
                 find_cluster_retry_delay=FIND_CLUSTER_RETRY_DELAY,
                 failure_retry_delay=FAILURE_RETRY_DELAY,
                 max_cluster_fails=MAX_CLUSTER_FAILS):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :param build_kwargs: dict, keyword arguments for starting worker builds
        :param worker_build_image: str, the builder image to use for worker builds
                                  (not used, image is inherited from the orchestrator)
        :param config_kwargs: dict, keyword arguments to override worker configuration
        :param find_cluster_retry_delay: the delay in seconds to try again reaching a cluster
        :param failure_retry_delay: the delay in seconds to try again starting a build
        :param max_cluster_fails: the maximum number of times a cluster can fail before being
                                  ignored
        """
        super(OrchestrateBuildPlugin, self).__init__(workflow)
        self.platforms = get_platforms(self.workflow)

        self.build_kwargs = build_kwargs or {}
        self.config_kwargs = config_kwargs or {}

        self.adjust_build_kwargs()
        self.adjust_config_kwargs()
        self.reactor_config = self.workflow.conf

        self.find_cluster_retry_delay = find_cluster_retry_delay
        self.failure_retry_delay = failure_retry_delay
        self.max_cluster_fails = max_cluster_fails
        self.koji_upload_dir = generate_koji_upload_dir()
        self.fs_task_id = self.get_fs_task_id()
        self.release = self.get_release()

        if worker_build_image:
            self.log.warning('worker_build_image is deprecated')

        self.worker_builds = []
        self.namespace = get_build_json().get('metadata', {}).get('namespace', None)
        self.build_image_digests = {}  # by platform
        self._openshift_session = None
        self.build_image_override = workflow.conf.build_image_override

    def adjust_config_kwargs(self):
        koji_map = self.workflow.conf.koji
        self.config_kwargs['koji_hub'] = koji_map['hub_url']
        self.config_kwargs['koji_root'] = koji_map['root_url']

        odcs_map = self.workflow.conf.odcs
        self.config_kwargs['odcs_url'] = odcs_map.get('api_url')
        self.config_kwargs['odcs_insecure'] = odcs_map.get('insecure', False)

        smtp_map = self.workflow.conf.smtp
        self.config_kwargs['smtp_additional_addresses'] =\
            ','.join(smtp_map.get('additional_addresses', ()))
        self.config_kwargs['smtp_email_domain'] = smtp_map.get('domain')
        self.config_kwargs['smtp_error_addresses'] = ','.join(smtp_map.get('error_addresses', ()))
        self.config_kwargs['smtp_from'] = smtp_map.get('from_address')
        self.config_kwargs['smtp_host'] = smtp_map.get('host')
        self.config_kwargs['smtp_to_pkgowner'] = smtp_map.get('send_to_pkg_owner', False)
        self.config_kwargs['smtp_to_submitter'] = smtp_map.get('send_to_submitter', False)

        source_registry = self.workflow.conf.source_registry['uri']
        self.config_kwargs['source_registry_uri'] = source_registry.uri if source_registry else None

        self.config_kwargs['artifacts_allowed_domains'] =\
            ','.join(self.workflow.conf.artifacts_allowed_domains)

        equal_labels = self.workflow.conf.image_equal_labels
        if equal_labels:
            equal_labels_sets = []
            for equal_set in equal_labels:
                equal_labels_sets.append(':'.join(equal_set))
            equal_labels_string = ','.join(equal_labels_sets)
            self.config_kwargs['equal_labels'] = equal_labels_string

        self.config_kwargs['prefer_schema1_digest'] = self.workflow.conf.prefer_schema1_digest

        self.config_kwargs['registry_api_versions'] = ','.join(self.workflow.conf.content_versions)

        self.config_kwargs['yum_proxy'] = self.workflow.conf.yum_proxy

        self.config_kwargs['sources_command'] = self.workflow.conf.sources_command

    def adjust_build_kwargs(self):
        # OSBS2 TBD
        self.build_kwargs['parent_images_digests'] = self.workflow.parent_images_digests
        # All platforms should generate the same operator manifests. We can use any of them
        if self.platforms:
            self.build_kwargs['operator_manifests_extract_platform'] = list(self.platforms)[0]

    def get_current_builds(self, osbs):
        finished_states = ["failed", "complete", "error", "cancelled"]
        field_selector = ','.join(['status!={status}'.format(status=status.capitalize())
                                   for status in finished_states])
        with osbs.retries_disabled():
            return len(osbs.list_builds(field_selector=field_selector))

    def _get_openshift_session(self, kwargs):
        conf = Configuration(**kwargs)
        return OSBS(conf)

    def get_cluster_info(self, cluster, platform):
        kwargs = deepcopy(self.config_kwargs)
        # config sections in osbs will be based on tasks, orchestrator/worker/source
        # kwargs['conf_section'] = cluster.name
        # we won't use anymore client-config as we will get all cluster information
        # from reactor-config-map
        # kwargs['conf_file'] = get_clusters_client_config_path(self.workflow)

        # we will pass to osbs.Configuration config based on rcm
        # kwargs = {
        # # we don't even want any default config
        # 'conf_file': None,
        # # use from rcm: openshift->build_json_dir
        # 'build_json_dir':  '/usr/share/osbs',
        # # use from rcm: worker_pipeline_clusters->aarch64[0]->namespace
        # 'namespace': 'worker',
        # # use from rcm: worker_pipeline_clusters->aarch64[0]->openshift_url
        # 'openshift_url': 'https://osbs.psi.redhat.com',
        # # use from rcm: worker_pipeline_clusters->aarch64[0]->token_file
        # 'token_file': '/workspace/ws-worker-tokens-secrets/x86-64-upshift-orchestrator'
        # # use from rcm: worker_pipeline_clusters->aarch64[0]->use_auth
        # 'use_auth': True,
        # # use from rcm: worker_pipeline_clusters->aarch64[0]->verify_ssl
        # 'verify_ssl': True,
        # }

        if platform in self.build_image_digests:
            kwargs['build_from'] = 'image:' + self.build_image_digests[platform]
        else:
            raise RuntimeError("build_image for platform '%s' not available" % platform)

        osbs = self._get_openshift_session(kwargs)

        current_builds = self.get_current_builds(osbs)

        load = current_builds / cluster.max_concurrent_builds
        self.log.debug('enabled cluster %s for platform %s has load %s and active builds %s/%s',
                       cluster.name, platform, load, current_builds, cluster.max_concurrent_builds)
        return ClusterInfo(cluster, platform, osbs, load)

    def get_clusters(self, platform, retry_contexts, all_clusters):
        ''' return clusters sorted by load. '''

        possible_cluster_info = {}
        candidates = set(copy.copy(all_clusters))
        while candidates and not possible_cluster_info:
            wait_for_any_cluster(retry_contexts)

            for cluster in sorted(candidates, key=attrgetter('priority')):
                ctx = retry_contexts[cluster.name]
                if ctx.in_retry_wait:
                    continue
                if ctx.failed:
                    continue
                try:
                    cluster_info = self.get_cluster_info(cluster, platform)
                    possible_cluster_info[cluster] = cluster_info
                except OsbsException:
                    ctx.try_again_later(self.find_cluster_retry_delay)
            candidates -= {c for c in candidates if retry_contexts[c.name].failed}

        ret = sorted(possible_cluster_info.values(), key=lambda c: c.cluster.priority)
        ret = sorted(ret, key=lambda c: c.load)
        return ret

    def get_release(self):
        labels = Labels(df_parser(self.workflow.df_path, workflow=self.workflow).labels)
        _, release = labels.get_name_and_value(Labels.LABEL_TYPE_RELEASE)
        return release

    def _update_content_versions(self, worker_reactor_conf, valid_values=('v2',)):
        if 'content_versions' not in worker_reactor_conf:
            return

        invalid_values = [v for v in worker_reactor_conf['content_versions']
                          if v not in valid_values]
        self.log.info('removing unsupported values "%s" from content_versions', invalid_values)
        worker_reactor_conf['content_versions'] = [v for v in
                                                   worker_reactor_conf['content_versions']
                                                   if v in valid_values]

        if not worker_reactor_conf['content_versions']:
            raise RuntimeError("content_versions is empty")

    def get_worker_build_kwargs(self, release, platform, koji_upload_dir,
                                task_id, worker_openshift):
        build_kwargs = deepcopy(self.build_kwargs)

        build_kwargs.pop('architecture', None)

        build_kwargs['release'] = release
        build_kwargs['platform'] = platform
        build_kwargs['koji_upload_dir'] = koji_upload_dir
        if task_id:
            build_kwargs['filesystem_koji_task_id'] = task_id

        if not self.reactor_config.is_default():
            worker_reactor_conf = deepcopy(self.reactor_config.conf)
            worker_reactor_conf['openshift'] = worker_openshift
            worker_reactor_conf.pop('worker_token_secrets', None)
            self._update_content_versions(worker_reactor_conf)

            build_kwargs['reactor_config_override'] = worker_reactor_conf

        return build_kwargs

    def get_fs_task_id(self):
        task_id = None

        fs_result = self.workflow.prebuild_results.get(PLUGIN_ADD_FILESYSTEM_KEY)
        if fs_result is None:
            return None

        try:
            task_id = int(fs_result['filesystem-koji-task-id'])
        except KeyError:
            self.log.error("%s: expected filesystem-koji-task-id in result",
                           PLUGIN_ADD_FILESYSTEM_KEY)
            raise
        except (ValueError, TypeError):
            self.log.exception("%s: returned an invalid task ID: %r",
                               PLUGIN_ADD_FILESYSTEM_KEY, task_id)
            raise

        self.log.debug("%s: got filesystem_koji_task_id of %d",
                       PLUGIN_ADD_FILESYSTEM_KEY, task_id)

        return task_id

    def do_worker_build(self, cluster_info):
        workspace = self.workflow.plugin_workspace.get(self.key, {})
        override_kwargs = workspace.get(WORKSPACE_KEY_OVERRIDE_KWARGS, {})

        build = None

        try:
            worker_openshift = {
                'url': cluster_info.osbs.build_conf.get_openshift_base_uri(),
                'build_json_dir': cluster_info.osbs.build_conf.get_builder_build_json_store(),
                'insecure': not cluster_info.osbs.build_conf.get_verify_ssl(),
                'auth': {
                    'enable': cluster_info.osbs.build_conf.get_use_auth(),
                }
            }
            kwargs = self.get_worker_build_kwargs(self.release, cluster_info.platform,
                                                  self.koji_upload_dir, self.fs_task_id,
                                                  worker_openshift)
            # Set overrides for all platforms
            if None in override_kwargs:
                kwargs.update(override_kwargs[None])
            # Set overrides for each platform, overriding any set for all platforms
            if cluster_info.platform in override_kwargs:
                self.log.debug("%s - overriding with %s", cluster_info.platform,
                               override_kwargs[cluster_info.platform])
                kwargs.update(override_kwargs[cluster_info.platform])
            with cluster_info.osbs.retries_disabled():
                build = cluster_info.osbs.create_worker_build(**kwargs)
        except OsbsException:
            self.log.exception('%s - failed to create worker build.',
                               cluster_info.platform)
            raise
        except Exception:
            self.log.exception('%s - failed to create worker build',
                               cluster_info.platform)

        build_info = WorkerBuildInfo(build=build, cluster_info=cluster_info, logger=self.log)
        self.worker_builds.append(build_info)

        if build_info.build:
            try:
                self.log.info('%s - created build %s on cluster %s.', cluster_info.platform,
                              build_info.name, cluster_info.cluster.name)
                build_info.watch_logs()
                build_info.wait_to_finish()
            except Exception as e:
                build_info.monitor_exception = e
                self.log.exception('%s - failed to monitor worker build',
                                   cluster_info.platform)

                # Attempt to cancel it rather than leave it running
                # unmonitored.
                try:
                    build_info.cancel_build()
                except OsbsException:
                    pass

    def select_and_start_cluster(self, platform):
        ''' Choose a cluster and start a build on it '''

        clusters = self.reactor_config.get_enabled_clusters_for_platform(platform)

        if not clusters:
            raise UnknownPlatformException('No clusters found for platform {}!'
                                           .format(platform))

        retry_contexts = {
            cluster.name: ClusterRetryContext(self.max_cluster_fails)
            for cluster in clusters
        }

        while True:
            try:
                possible_cluster_info = self.get_clusters(platform,
                                                          retry_contexts,
                                                          clusters)
            except AllClustersFailedException as ex:
                cluster = ClusterInfo(None, platform, None, None)
                build_info = WorkerBuildInfo(build=None,
                                             cluster_info=cluster,
                                             logger=self.log)
                build_info.monitor_exception = str(ex)
                self.worker_builds.append(build_info)
                return

            for cluster_info in possible_cluster_info:
                ctx = retry_contexts[cluster_info.cluster.name]
                try:
                    self.log.info('Attempting to start build for platform %s on cluster %s',
                                  platform, cluster_info.cluster.name)
                    self.do_worker_build(cluster_info)
                    return
                except OsbsException:
                    ctx.try_again_later(self.failure_retry_delay)
                    # this will put the cluster in retry-wait when get_clusters runs

    @property
    def openshift_session(self):
        if not self._openshift_session:
            self._openshift_session = \
                get_openshift_session(self.workflow.conf,
                                      self.workflow.user_params.get('namespace'))

        return self._openshift_session

    def get_current_buildimage(self):
        spec = get_build_json().get("spec")
        try:
            build_name = spec['strategy']['customStrategy']['from']['name']
            build_kind = spec['strategy']['customStrategy']['from']['kind']
        except KeyError as exc:
            raise RuntimeError(
                "Build object is malformed, failed to fetch buildroot image"
            ) from exc

        if build_kind == 'DockerImage':
            return build_name
        else:
            raise RuntimeError("Build kind isn't 'DockerImage' but %s" % build_kind)

    def process_image_from(self, image_from):
        build_image = None
        imagestream = None

        if image_from['kind'] == 'DockerImage':
            build_image = image_from['name']
        elif image_from['kind'] == 'ImageStreamTag':
            imagestream = image_from['name']
        else:
            raise UnknownKindException

        return build_image, imagestream

    def check_manifest_list(self, build_image, orchestrator_platform, platforms,
                            current_buildimage):
        registry_name, image = build_image.split('/', 1)
        repo, tag = image.rsplit(':', 1)

        registry = ImageName(registry=registry_name, repo=repo, tag=tag)
        manifest_list = get_manifest_list(registry, registry_name, insecure=True)

        # we don't have manifest list, but we want to build on different platforms
        if not manifest_list:
            raise RuntimeError("Buildroot image isn't manifest list,"
                               " which is needed for specified arch")
        arch_digests = {}
        image_name = build_image.rsplit(':', 1)[0]

        manifest_list_dict = manifest_list.json()
        for manifest in manifest_list_dict['manifests']:
            arch = manifest['platform']['architecture']
            arch_digests[arch] = image_name + '@' + manifest['digest']

        arch_to_platform = self.workflow.conf.goarch_to_platform_mapping
        for arch, image in arch_digests.items():
            self.build_image_digests[arch_to_platform[arch]] = image

        # orchestrator platform is in manifest list
        if orchestrator_platform not in self.build_image_digests:
            raise RuntimeError("Platform for orchestrator '%s' isn't in manifest list"
                               % orchestrator_platform)

        if ('@sha256:' in current_buildimage and
                self.build_image_digests[orchestrator_platform] != current_buildimage):
            raise RuntimeError("Orchestrator is using image digest '%s' which isn't"
                               " in manifest list" % current_buildimage)

    def get_image_info_from_annotations(self):
        annotations = get_build_json().get("metadata", {}).get('annotations', {})
        if 'from' in annotations:
            scratch_from = json.loads(annotations['from'])

            try:
                return self.process_image_from(scratch_from)
            except UnknownKindException as exc:
                raise RuntimeError(
                    "Build annotation has unknown 'kind' %s" % scratch_from['kind']
                ) from exc
        else:
            raise RuntimeError("Build wasn't created from BuildConfig and neither"
                               " has 'from' annotation, which is needed for specified arch")

    def get_build_image_from_imagestream(self, imagestream):
        try:
            tag = self.openshift_session.get_image_stream_tag(imagestream).json()
        except OsbsException as exc:
            raise RuntimeError("ImageStreamTag not found %s" % imagestream) from exc

        try:
            tag_image = tag['image']['dockerImageReference']
        except KeyError as exc:
            raise RuntimeError("ImageStreamTag is malformed %s" % imagestream) from exc

        if '@sha256:' in tag_image:
            try:
                labels = tag['image']['dockerImageMetadata']['Config']['Labels']
            except KeyError as exc:
                raise RuntimeError(
                    "Image in imageStreamTag '%s' is missing Labels" % imagestream
                ) from exc

            release = labels['release']
            version = labels['version']
            docker_tag = "%s-%s" % (version, release)
            return tag_image[:tag_image.find('@sha256')] + ':' + docker_tag
        else:
            return tag_image

    def set_build_image(self):
        """
        Overrides build_image for worker, to be same as in orchestrator build
        """
        current_platform = platform.processor()
        orchestrator_platform = current_platform or 'x86_64'
        current_buildimage = self.get_current_buildimage()

        for plat, build_image in self.build_image_override.items():
            self.log.debug('Overriding build image for %s platform to %s',
                           plat, build_image)
            self.build_image_digests[plat] = build_image

        manifest_list_platforms = self.platforms - set(self.build_image_override.keys())
        if not manifest_list_platforms:
            self.log.debug('Build image override used for all platforms, '
                           'skipping build image manifest list checks')
            return

        # orchestrator platform is same as platform on which we want to built on,
        # so we can use the same image
        if manifest_list_platforms == {orchestrator_platform}:
            self.build_image_digests[orchestrator_platform] = current_buildimage
            return

        # get image build from build metadata, which is set for direct builds
        # this is explicitly set by osbs-client, it isn't default OpenShift behaviour
        build_image, imagestream = self.get_image_info_from_annotations()

        # if imageStream is used
        if imagestream:
            build_image = self.get_build_image_from_imagestream(imagestream)

        # we have build_image with tag, so we can check for manifest list
        if build_image:
            self.check_manifest_list(build_image, orchestrator_platform,
                                     manifest_list_platforms, current_buildimage)

    def run(self):
        if not self.platforms:
            raise RuntimeError("No enabled platform to build on")
        self.set_build_image()

        thread_pool = ThreadPool(len(self.platforms))
        result = thread_pool.map_async(self.select_and_start_cluster, self.platforms)

        try:
            result.get()
        # Always clean up worker builds on any error to avoid
        # runaway worker builds (includes orchestrator build cancellation)
        except Exception:
            thread_pool.terminate()
            self.log.info('build cancelled, cancelling worker builds')
            if self.worker_builds:
                ThreadPool(len(self.worker_builds)).map(
                    lambda bi: bi.cancel_build(), self.worker_builds)
            while not result.ready():
                result.wait(1)
            raise
        else:
            thread_pool.close()
            thread_pool.join()

        annotations = {'worker-builds': {
            build_info.platform: build_info.get_annotations()
            for build_info in self.worker_builds if build_info.build
        }}

        fail_reasons = {
            build_info.platform: build_info.get_fail_reason()
            for build_info in self.worker_builds
            if not build_info.build or not build_info.build.is_succeeded()
        }

        workspace = self.workflow.plugin_workspace.setdefault(self.key, {})
        workspace[WORKSPACE_KEY_UPLOAD_DIR] = self.koji_upload_dir
        workspace[WORKSPACE_KEY_BUILD_INFO] = {build_info.platform: build_info
                                               for build_info in self.worker_builds}

        if fail_reasons:
            return BuildResult(fail_reason=json.dumps(fail_reasons),
                               annotations=annotations)

        return BuildResult.make_remote_image_result(annotations)
