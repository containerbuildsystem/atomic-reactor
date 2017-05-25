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

import json
import os

from atomic_reactor.build import BuildResult
from atomic_reactor.plugin import BuildStepPlugin
from atomic_reactor.plugins.pre_reactor_config import get_config
from atomic_reactor.util import get_preferred_label, df_parser
from osbs.api import OSBS
from osbs.conf import Configuration
from osbs.constants import BUILD_FINISHED_STATES


ClusterInfo = namedtuple('ClusterInfo', ('cluster', 'platform', 'osbs', 'load'))


class WorkerBuildInfo(object):

    def __init__(self, build, cluster_info):
        self.build = build
        self.cluster = cluster_info.cluster
        self.osbs = cluster_info.osbs
        self.platform = cluster_info.platform

        self.monitor_exception = None

    @property
    def name(self):
        return self.build.get_build_name() if self.build else 'N/A'

    def wait_to_finish(self):
        self.build = self.osbs.wait_for_build_to_finish(self.name)
        return self.build

    def watch_logs(self, logger):
        for line in self.osbs.get_build_logs(self.name, follow=True):
            # TODO: This is a little clunky:
            # 2017-02-24 14:22:51,472 - atomic_reactor.plugins.orchestrate_build - INFO - x86_64 - 2017-02-24 14:22:51,314 - atomic_reactor.plugin - INFO - <worker build log message>
            logger.info('%s - %s', self.platform, line)

    def get_annotations(self):
        build_annotations = self.build.get_annotations() or {}
        return {
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

    def get_fail_reason(self):
        if not self.build:
            return {'general': 'build not started'}
        build_annotations = self.build.get_annotations() or {}
        metadata = json.loads(build_annotations.get('plugins-metadata', '{}'))
        fail_reason = metadata.get('errors', {})

        if self.monitor_exception:
            fail_reason['general'] = repr(self.monitor_exception)

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

    EXCLUDE_FILENAME = 'exclude-platform'

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

    def get_excluded_platforms(self):
        df_dir = self.workflow.source.get_dockerfile_path()[1]
        exclude_platforms = set()
        exclude_path = os.path.join(df_dir, self.EXCLUDE_FILENAME)
        if os.path.exists(exclude_path):
            with open(exclude_path) as f:
                for platform in f:
                    platform = platform.strip()
                    if not platform:
                        continue
                    exclude_platforms.add(platform)

        return exclude_platforms

    def get_platforms(self):
        return self.platforms - self.get_excluded_platforms()

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
        current_builds = self.get_current_builds(osbs)
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

        selected = min(clusters, key=lambda c: c.load)
        self.log.info('platform %s will use cluster %s',
                      platform, selected.cluster.name)
        return selected

    def get_release(self):
        labels = df_parser(self.workflow.builder.df_path, workflow=self.workflow).labels
        return get_preferred_label(labels, 'release')

    def get_worker_build_kwargs(self, release, platform):
        build_kwargs = deepcopy(self.build_kwargs)

        build_kwargs.pop('architecture', None)

        build_kwargs['release'] = release
        build_kwargs['platform'] = platform

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

    def do_worker_build(self, release, cluster_info):
        build = None
        try:
            kwargs = self.get_worker_build_kwargs(release, cluster_info.platform)
            build = cluster_info.osbs.create_worker_build(**kwargs)
        except Exception:
            self.log.exception('%s - failed to create worker build',
                               cluster_info.platform)

        build_info = WorkerBuildInfo(build=build, cluster_info=cluster_info)
        self.worker_builds.append(build_info)

        if build_info.build:
            try:
                self.log.info('%s - created build %s', cluster_info.platform,
                              build_info.name)
                build_info.watch_logs(self.log)
                build_info.wait_to_finish()
            except Exception as e:
                build_info.monitor_exception = e
                self.log.exception('%s - failed to monitor worker build',
                                   cluster_info.platform)

    def run(self):
        release = self.get_release()
        platforms = self.get_platforms()

        thread_pool = ThreadPool(len(platforms))
        result = thread_pool.map_async(
            lambda cluster_info: self.do_worker_build(release, cluster_info),
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

        if fail_reasons:
            return BuildResult(fail_reason=json.dumps(fail_reasons),
                               annotations=annotations, labels=labels)

        return BuildResult.make_remote_image_result(annotations, labels=labels)
