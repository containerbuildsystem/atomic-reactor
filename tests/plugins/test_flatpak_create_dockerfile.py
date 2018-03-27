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
    from atomic_reactor.plugins.pre_flatpak_create_dockerfile import FlatpakCreateDockerfilePlugin
except ImportError:
    pass

from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.source import VcsInfo
from atomic_reactor.util import ImageName

from tests.constants import (MOCK_SOURCE, FLATPAK_GIT, FLATPAK_SHA1)
from tests.fixtures import docker_tasker  # noqa
from tests.flatpak import MODULEMD_AVAILABLE, build_flatpak_test_configs, setup_flatpak_compose_info


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


def mock_workflow(tmpdir, container_yaml):
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    mock_source = MockSource(tmpdir)
    setattr(workflow, 'builder', MockBuilder())
    workflow.builder.source = mock_source
    flexmock(workflow, source=mock_source)

    with open(mock_source.container_yaml_path, "w") as f:
        f.write(container_yaml)

    setattr(workflow.builder, 'df_dir', str(tmpdir))

    return workflow


CONFIGS = build_flatpak_test_configs()


@responses.activate  # noqa - docker_tasker fixture
@pytest.mark.skipif(not MODULEMD_AVAILABLE,
                    reason='modulemd not available')
@pytest.mark.parametrize('config_name,breakage', [
    ('app', None),
    ('runtime', None),
    ('runtime', 'branch_mismatch'),
])
def test_flatpak_create_dockerfile(tmpdir, docker_tasker, config_name, breakage):
    config = CONFIGS[config_name]

    workflow = mock_workflow(tmpdir, config['container_yaml'])

    compose = setup_flatpak_compose_info(workflow, config)

    if breakage == 'branch_mismatch':
        compose.base_module.mmd.xmd['flatpak']['branch'] = 'MISMATCH'
        expected_exception = "Mismatch for 'branch'"
    else:
        assert breakage is None
        expected_exception = None

    args = {
        'base_image': "registry.fedoraproject.org/fedora:latest",
    }

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FlatpakCreateDockerfilePlugin.key,
            'args': args
        }]
    )

    if expected_exception:
        with pytest.raises(PluginFailedException) as ex:
            runner.run()
        assert expected_exception in str(ex)
    else:
        runner.run()

        assert os.path.exists(workflow.builder.df_path)

        includepkgs_path = os.path.join(workflow.builder.df_dir, 'atomic-reactor-includepkgs')
        assert os.path.exists(includepkgs_path)
        with open(includepkgs_path) as f:
            includepkgs = f.read()
            assert 'librsvg2' in includepkgs
            if config_name == 'app':
                assert 'eog-0:3.24.1-1.module_7b96ed10.x86_64' in includepkgs

        assert os.path.exists(os.path.join(workflow.builder.df_dir, 'cleanup.sh'))
