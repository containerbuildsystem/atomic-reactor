"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals, absolute_import

import os
import logging

from flexmock import flexmock

from atomic_reactor.core import DockerTasker
from atomic_reactor.constants import PLUGIN_REMOVE_WORKER_METADATA_KEY
from atomic_reactor.plugin import ExitPluginsRunner
from atomic_reactor.util import ImageName
from atomic_reactor.build import BuildResult
from osbs.exceptions import OsbsResponseException
from atomic_reactor.plugins.build_orchestrate_build import (WorkerBuildInfo, ClusterInfo,
                                                            OrchestrateBuildPlugin)

from tests.constants import INPUT_IMAGE
from tests.docker_mock import mock_docker
import pytest


class MockConfigMapResponse(object):
    def __init__(self, data):
        self.data = data

    def get_data_by_key(self, key):
        return self.data[key]


class MockOSBS(object):
    def delete_config_map(self, name):
        return name


class MockSource(object):

    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = os.path.join(tmpdir, 'Dockerfile')
        self.path = tmpdir

    def get_dockerfile_path(self):
        return self.dockerfile_path, self.path


class MockInsideBuilder(object):

    def __init__(self):
        mock_docker()
        self.tasker = DockerTasker()
        self.base_image = ImageName(repo='fedora', tag='25')
        self.image_id = 'image_id'
        self.image = INPUT_IMAGE
        self.df_path = 'df_path'
        self.df_dir = 'df_dir'

        def simplegen(x, y):
            yield "some\u2018".encode('utf-8')
        flexmock(self.tasker, build_image_from_path=simplegen)

    def get_built_image_info(self):
        return {'Id': 'some'}

    def inspect_built_image(self):
        return None

    def ensure_not_built(self):
        pass


def mock_workflow(tmpdir, workflow):
    setattr(workflow, 'builder', MockInsideBuilder())
    setattr(workflow, 'source', MockSource(tmpdir))
    setattr(workflow.builder, 'source', MockSource(tmpdir))

    return workflow


@pytest.mark.parametrize('platforms', [['x86_64'],
                                       ['ppc64le'],
                                       ['x86_64', 'ppc64le'],
                                       [None]])
@pytest.mark.parametrize('fragment_annotation', [True, False])
@pytest.mark.parametrize('fragment_key', ['metadata.json', None])
@pytest.mark.parametrize('cm_not_found', [True, False])
def test_remove_worker_plugin(tmpdir, workflow, caplog, platforms, fragment_annotation,
                              fragment_key, cm_not_found):
    workflow = mock_workflow(tmpdir, workflow)

    annotations = {'worker-builds': {}}
    log = logging.getLogger("atomic_reactor.plugins." + OrchestrateBuildPlugin.key)
    build = None
    cluster = None
    load = None
    workspace = {
        'build_info': {},
        'koji_upload_dir': 'foo',
    }

    for platform in platforms:
        build_name = 'build-1-%s' % platform
        metadata_fragment = None
        if platform:
            config_name = 'build-1-%s-md' % platform
            metadata_fragment = 'configmap/%s' % config_name

            osbs = MockOSBS()
            cluster_info = ClusterInfo(cluster, platform, osbs, load)
            worker_info = WorkerBuildInfo(build, cluster_info, log)
            workspace['build_info'][platform] = worker_info

            if fragment_key and fragment_annotation:
                if cm_not_found:
                    (flexmock(osbs)
                     .should_receive("delete_config_map")
                     .with_args(config_name)
                     .once()
                     .and_raise(OsbsResponseException('none', 404)))
                else:
                    (flexmock(osbs)
                     .should_receive("delete_config_map")
                     .with_args(config_name)
                     .once()
                     .and_return(True))

        annotations['worker-builds'][platform] = {'build': {'build-name': build_name}}
        if fragment_annotation:
            annotations['worker-builds'][platform]['metadata_fragment'] = metadata_fragment
            annotations['worker-builds'][platform]['metadata_fragment_key'] = fragment_key

    workflow.build_result = BuildResult(annotations=annotations, image_id="id1234")
    workflow.plugin_workspace[OrchestrateBuildPlugin.key] = workspace

    runner = ExitPluginsRunner(
        None,
        workflow,
        [{
            'name': PLUGIN_REMOVE_WORKER_METADATA_KEY,
            "args": {}
        }]
    )

    runner.run()

    for platform in platforms:
        if platform and fragment_key:
            cm_name = 'build-1-%s-md' % platform
            if not fragment_annotation:
                continue
            if cm_not_found:
                msg = "Failed to delete ConfigMap {} on platform {}:".format(cm_name, platform)
                assert msg in caplog.text
            else:
                msg = "ConfigMap {} on platform {} deleted". format(cm_name, platform)
                assert msg in caplog.text
