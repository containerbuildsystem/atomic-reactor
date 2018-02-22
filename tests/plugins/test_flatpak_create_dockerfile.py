"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from flexmock import flexmock

import responses
import os
import pytest

from atomic_reactor.inner import DockerBuildWorkflow
try:
    from atomic_reactor.plugins.pre_resolve_module_compose import (ComposeInfo,
                                                                   ModuleInfo,
                                                                   set_compose_info)
    from atomic_reactor.plugins.pre_flatpak_create_dockerfile import FlatpakCreateDockerfilePlugin
    from modulemd import ModuleMetadata
    MODULEMD_AVAILABLE = True
except ImportError:
    MODULEMD_AVAILABLE = False

from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.source import VcsInfo
from atomic_reactor.util import ImageName

from tests.constants import (MOCK_SOURCE, FLATPAK_GIT, FLATPAK_SHA1)
from tests.fixtures import docker_tasker  # noqa
from tests.flatpak import FLATPAK_APP_CONTAINER_YAML, FLATPAK_APP_MODULEMD, FLATPAK_APP_RPMS


class MockSource(object):
    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = "./"
        self.path = tmpdir

        self.container_yaml_path = os.path.join(tmpdir, 'container.yaml')

    def get_build_file_path(self):
        return self.container_yaml_path, self.path

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

    with open(mock_source.container_yaml_path, "w") as f:
        f.write(FLATPAK_APP_CONTAINER_YAML)

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
@pytest.mark.skipif(not MODULEMD_AVAILABLE,
                    reason='modulemd not available')
def test_flatpak_create_dockerfile(tmpdir, docker_tasker):
    workflow = mock_workflow(tmpdir)

    args = {
        'base_image': "registry.fedoraproject.org/fedora:latest",
    }

    mmd = ModuleMetadata()
    mmd.loads(FLATPAK_APP_MODULEMD)

    base_module = ModuleInfo(MODULE_NAME, MODULE_STREAM, LATEST_VERSION,
                             mmd, FLATPAK_APP_RPMS)
    repo_url = 'http://odcs.example/composes/latest-odcs-42-1/compose/Temporary/$basearch/os/'
    compose_info = ComposeInfo(MODULE_STREAM + '-' + MODULE_STREAM,
                               42, base_module,
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
