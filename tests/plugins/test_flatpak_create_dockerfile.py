"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from flexmock import flexmock

import json
from modulemd import ModuleMetadata
import responses
import os

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugins.pre_resolve_module_compose import (ComposeInfo,
                                                               ModuleInfo,
                                                               set_compose_info)
from atomic_reactor.plugins.pre_flatpak_create_dockerfile import FlatpakCreateDockerfilePlugin
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.source import VcsInfo
from atomic_reactor.util import ImageName

from tests.constants import (MOCK_SOURCE, FLATPAK_GIT, FLATPAK_SHA1)
from tests.fixtures import docker_tasker  # noqa
from tests.flatpak import FLATPAK_APP_JSON, FLATPAK_APP_MODULEMD


class MockSource(object):
    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = "./"
        self.path = tmpdir

        self.flatpak_json_path = os.path.join(tmpdir, 'flatpak.json')

    def get_build_file_path(self):
        return self.flatpak_json_path, self.path

    def get_vcs_info(self):
        return VcsInfo('git', FLATPAK_GIT, FLATPAK_SHA1)


class MockBuilder(object):
    def __init__(self):
        self.image_id = "xxx"
        self.base_image = ImageName.parse("org.gnome.eog")

    def set_base_image(self, base_image):
        pass

    def set_df_path(self, path):
        self.df_path = path


def mock_workflow(tmpdir):
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    mock_source = MockSource(tmpdir)
    setattr(workflow, 'builder', MockBuilder())
    workflow.builder.source = mock_source
    flexmock(workflow, source=mock_source)

    with open(mock_source.flatpak_json_path, "w") as f:
        f.write(json.dumps(FLATPAK_APP_JSON))

    setattr(workflow.builder, 'df_dir', str(tmpdir))

    return workflow


PDC_URL = 'https://pdc.fedoraproject.org/rest_api/v1'
MODULE_NAME = 'eog'
MODULE_STREAM = 'f26'

ALL_VERSIONS_JSON = [{"variant_release": "20170629143459"},
                     {"variant_release": "20170629213428"}]

LATEST_VERSION = "20170629213428"
LATEST_VERSION_JSON = [{"modulemd": FLATPAK_APP_MODULEMD}]


@responses.activate  # noqa - docker_tasker fixture
def test_flatpak_create_dockerfile(tmpdir, docker_tasker):
    workflow = mock_workflow(tmpdir)

    args = {
        'base_image': "registry.fedoraproject.org/fedora:latest",
    }

    mmd = ModuleMetadata()
    mmd.loads(FLATPAK_APP_MODULEMD)

    base_module = ModuleInfo(MODULE_NAME, MODULE_STREAM, LATEST_VERSION,
                             mmd)
    repo_url = 'http://odcs.example/composes/latest-odcs-42-1/compose/Temporary/$basearch/os/'
    compose_info = ComposeInfo(42, base_module,
                               {'eog': base_module},
                               repo_url)
    set_compose_info(workflow, compose_info)

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FlatpakCreateDockerfilePlugin.key,
            'args': args
        }]
    )

    runner.run()

    assert os.path.exists(workflow.builder.df_path)
    assert os.path.exists(os.path.join(workflow.builder.df_dir, 'cleanup.sh'))
