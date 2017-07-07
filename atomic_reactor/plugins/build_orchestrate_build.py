"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals, division

from collections import namedtuple
from copy import deepcopy
from multiprocessing.pool import ThreadPool

import yaml
import json
import os
import random
from string import ascii_letters
import time
import logging

from atomic_reactor.build import BuildResult
from atomic_reactor.plugin import BuildStepPlugin, BuildCanceledException
from atomic_reactor.plugins.pre_reactor_config import get_config
from atomic_reactor.util import get_preferred_label, df_parser
from atomic_reactor.constants import PLUGIN_ADD_FILESYSTEM_KEY
from osbs.api import OSBS
from osbs.exceptions import OsbsException
from osbs.conf import Configuration
from osbs.constants import BUILD_FINISHED_STATES


ClusterInfo = namedtuple('ClusterInfo', ('cluster', 'platform', 'osbs', 'load'))
WORKSPACE_KEY_BUILD_INFO = 'build_info'
WORKSPACE_KEY_UPLOAD_DIR = 'koji_upload_dir'


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
        for line in self.osbs.get_build_logs(self.name, follow=True):
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
        if not self.build:
            return {'general': 'build not started'}
        build_annotations = self.build.get_annotations() or {}
        metadata = json.loads(build_annotations.get('plugins-metadata', '{}'))
        fail_reason = {}
        if self.monitor_exception:
            fail_reason['general'] = repr(self.monitor_exception)

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

    CONTAINER_FILENAME = 'container.yaml'
    UNREACHABLE_CLUSTER_LOAD = object()

    key = 'orchestrate_build'

    def __init__(self, tasker, workflow, platforms, build_kwargs,
                 osbs_client_config=None, worker_build_image=None,
                 config_kwargs=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param platforms: list<str>, platforms to build
        :param build_kwargs: dict, keyword arguments for starting worker builds
        :param osbs_client_config: str, path to directory containing osbs.conf
        :param worker_build_image: str, the builder image to use for worker builds
                                  (deprecated, use config_kwargs instead)
        :param config_kwargs: dict, keyword arguments to override worker configuration
        """
        super(OrchestrateBuildPlugin, self).__init__(tasker, workflow)
        self.platforms = set(platforms)
        self.build_kwargs = build_kwargs
        self.osbs_client_config = osbs_client_config
        self.config_kwargs = config_kwargs or {}

        if worker_build_image:
            self.log.warning('worker_build_image is deprecated, use config_kwargs instead')
            self.config_kwargs.setdefault('build_image', worker_build_image)

        self.worker_builds = []

    def make_list(self, value):
        if not isinstance(value, list):
            value = [value]
        return value

    def get_platforms(self):
        df_dir = self.workflow.source.get_dockerfile_path()[1]
        excluded_platforms = set()
        container_path = os.path.join(df_dir, self.CONTAINER_FILENAME)
        if os.path.exists(container_path):
            with open(container_path) as f:
                data = yaml.load(f)
                if data is None or 'platforms' not in data or data['platforms'] is None:
                    return self.platforms
                excluded_platforms = set(self.make_list(data['platforms'].get('not', [])))
                only_platforms = set(self.make_list(data['platforms'].get('only', [])))
                if only_platforms:
                    self.platforms = self.platforms & only_platforms
        return self.platforms - excluded_platforms

    def get_current_builds(self, osbs):
        field_selector = ','.join(['status!={status}'.format(status=status.capitalize())
                                   for status in BUILD_FINISHED_STATES])
        return len(osbs.list_builds(field_selector=field_selector))

    def get_cluster_info(self, cluster, platform):
        kwargs = deepcopy(self.config_kwargs)
        kwargs['conf_section'] = cluster.name
        if self.osbs_client_config:
            kwargs['conf_file'] = os.path.join(self.osbs_client_config, 'osbs.conf')

        conf = Configuration(**kwargs)
        osbs = OSBS(conf, conf)
        try:
            current_builds = self.get_current_builds(osbs)
        except OsbsException as e:
            # If the build is canceled reraise the error
            if isinstance(e.cause, BuildCanceledException):
                raise e

            self.log.exception("Error occurred while listing builds on %s",
                               cluster.name)
            return ClusterInfo(cluster, platform, osbs, self.UNREACHABLE_CLUSTER_LOAD)

        load = current_builds / cluster.max_concurrent_builds
        self.log.debug('enabled cluster %s for platform %s has load %s and active builds %s/%s',
                       cluster.name, platform, load, current_builds, cluster.max_concurrent_builds)
        return ClusterInfo(cluster, platform, osbs, load)

    def choose_cluster(self, platform):
        config = get_config(self.workflow)
        clusters = [self.get_cluster_info(cluster, platform) for cluster in
                    config.get_enabled_clusters_for_platform(platform)]

        if not clusters:
            raise RuntimeError('No clusters found for platform {}!'
                               .format(platform))

        reachable_clusters = [cluster for cluster in clusters
                              if cluster.load != self.UNREACHABLE_CLUSTER_LOAD]

        if not reachable_clusters:
            raise RuntimeError('All clusters for platform {} are unreachable!'
                               .format(platform))

        selected = min(reachable_clusters, key=lambda c: c.load)
        self.log.info('platform %s will use cluster %s',
                      platform, selected.cluster.name)
        return selected

    def get_release(self):
        labels = df_parser(self.workflow.builder.df_path, workflow=self.workflow).labels
        return get_preferred_label(labels, 'release')

    @staticmethod
    def get_koji_upload_dir():
        """
        Create a path name for uploading files to

        :return: str, path name expected to be unique
        """
        dir_prefix = 'koji-upload'
        random_chars = ''.join([random.choice(ascii_letters)
                                for _ in range(8)])
        unique_fragment = '%r.%s' % (time.time(), random_chars)
        return os.path.join(dir_prefix, unique_fragment)

    def get_worker_build_kwargs(self, release, platform, koji_upload_dir,
                                task_id):
        build_kwargs = deepcopy(self.build_kwargs)

        build_kwargs.pop('architecture', None)

        build_kwargs['release'] = release
        build_kwargs['platform'] = platform
        build_kwargs['koji_upload_dir'] = koji_upload_dir
        if task_id:
            build_kwargs['filesystem_koji_task_id'] = task_id

        return build_kwargs

    def _apply_repositories(self, annotations):
        unique = set()
        primary = set()

        for build_info in self.worker_builds:
            if not build_info.build:
                continue
            repositories = build_info.build.get_repositories() or {}
            unique.update(repositories.get('unique', []))
            primary.update(repositories.get('primary', []))

        if unique or primary:
            annotations['repositories'] = {
                'unique': sorted(list(unique)),
                'primary': sorted(list(primary)),
            }

    def _make_labels(self):
        labels = {}
        koji_build_id = None
        ids = set([build_info.build.get_koji_build_id()
                   for build_info in self.worker_builds
                   if build_info.build])
        self.log.debug('all koji-build-ids: %s', ids)
        if ids:
            koji_build_id = ids.pop()

        if koji_build_id:
            labels['koji-build-id'] = koji_build_id

        return labels

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

    def do_worker_build(self, release, cluster_info, koji_upload_dir, task_id):
        build = None

        try:
            kwargs = self.get_worker_build_kwargs(release, cluster_info.platform,
                                                  koji_upload_dir, task_id)
            build = cluster_info.osbs.create_worker_build(**kwargs)
        except Exception:
            self.log.exception('%s - failed to create worker build',
                               cluster_info.platform)

        build_info = WorkerBuildInfo(build=build, cluster_info=cluster_info, logger=self.log)
        self.worker_builds.append(build_info)

        if build_info.build:
            try:
                self.log.info('%s - created build %s', cluster_info.platform,
                              build_info.name)
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

    def run(self):
        release = self.get_release()
        platforms = self.get_platforms()
        koji_upload_dir = self.get_koji_upload_dir()
        task_id = self.get_fs_task_id()

        thread_pool = ThreadPool(len(platforms))
        result = thread_pool.map_async(
            lambda cluster_info: self.do_worker_build(release, cluster_info,
                                                      koji_upload_dir, task_id),
            [self.choose_cluster(platform) for platform in platforms]
        )

        try:
            while not result.ready():
                # The wait call is a blocking call which prevents signals
                # from being processed. Wait for short intervals instead
                # of a single long interval, so build cancellation can
                # be handled virtually immediately.
                result.wait(1)
        # Always clean up worker builds on any error to avoid
        # runaway worker builds (includes orchestrator build cancellation)
        except Exception:
            self.log.info('build cancelled, cancelling worker builds')
            if self.worker_builds:
                ThreadPool(len(self.worker_builds)).map(
                    lambda bi: bi.cancel_build(), self.worker_builds)
            while not result.ready():
                result.wait(1)
            raise

        annotations = {'worker-builds': {
            build_info.platform: build_info.get_annotations()
            for build_info in self.worker_builds if build_info.build
        }}

        self._apply_repositories(annotations)

        labels = self._make_labels()

        fail_reasons = {
            build_info.platform: build_info.get_fail_reason()
            for build_info in self.worker_builds
            if not build_info.build or not build_info.build.is_succeeded()
        }

        self.workflow.plugin_workspace[self.key] = {
            WORKSPACE_KEY_UPLOAD_DIR: koji_upload_dir,
            WORKSPACE_KEY_BUILD_INFO: {build_info.platform: build_info
                                       for build_info in self.worker_builds},
        }

        if fail_reasons:
            return BuildResult(fail_reason=json.dumps(fail_reasons),
                               annotations=annotations, labels=labels)

        return BuildResult.make_remote_image_result(annotations, labels=labels)
