"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import

from flexmock import flexmock

import responses
import os
import pytest
import re

from atomic_reactor.inner import DockerBuildWorkflow
try:
    from atomic_reactor.plugins.pre_flatpak_create_dockerfile import FlatpakCreateDockerfilePlugin
except ImportError:
    pass

from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor.source import VcsInfo, SourceConfig
from atomic_reactor.util import ImageName

from tests.constants import (MOCK_SOURCE, FLATPAK_GIT, FLATPAK_SHA1)
from tests.flatpak import MODULEMD_AVAILABLE, build_flatpak_test_configs, setup_flatpak_compose_info

if MODULEMD_AVAILABLE:
    from gi.repository import GLib


class MockSource(object):
    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = "./"
        self.path = tmpdir

        self.container_yaml_path = os.path.join(tmpdir, 'container.yaml')
        self.config = None

    def get_build_file_path(self):
        return self.container_yaml_path, self.path

    def get_vcs_info(self):
        return VcsInfo('git', FLATPAK_GIT, FLATPAK_SHA1)


class MockBuilder(object):
    def __init__(self):
        self.image_id = "xxx"
        self.base_image = ImageName.parse("org.gnome.eog")
        self.df_path = None

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
    workflow.builder.source.config = SourceConfig(str(tmpdir))

    setattr(workflow.builder, 'df_dir', str(tmpdir))

    return workflow


CONFIGS = build_flatpak_test_configs()


@responses.activate  # noqa - docker_tasker fixture
@pytest.mark.skipif(not MODULEMD_AVAILABLE,
                    reason='libmodulemd not available')
@pytest.mark.parametrize('config_name,breakage', [
    ('app', None),
    ('runtime', None),
    ('runtime', 'branch_mismatch'),
])
def test_flatpak_create_dockerfile(tmpdir, docker_tasker, config_name, breakage,
                                   reactor_config_map):
    config = CONFIGS[config_name]

    workflow = mock_workflow(tmpdir, config['container_yaml'])

    compose = setup_flatpak_compose_info(workflow, config)

    if breakage == 'branch_mismatch':
        xmd = compose.base_module.mmd.props.xmd

        # Modifying the xmd from Python requires creating a new GVariant
        flatpak_xmd = xmd['flatpak']

        new_flatpak_xmd = {}
        for i in range(flatpak_xmd.n_children()):
            v = flatpak_xmd.get_child_value(i)
            new_flatpak_xmd[v.get_child_value(0).unpack()] = v.get_child_value(1)

        new_flatpak_xmd['branch'] = GLib.Variant('s', 'MISMATCH')

        xmd['flatpak'] = GLib.Variant('a{sv}', new_flatpak_xmd)
        compose.base_module.mmd.props.xmd = xmd

        expected_exception = "Mismatch for 'branch'"
    else:
        assert breakage is None
        expected_exception = None

    base_image = "registry.fedoraproject.org/fedora:latest"

    args = {
        'base_image': base_image,
    }

    if reactor_config_map:
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {
            WORKSPACE_CONF_KEY: ReactorConfig({'version': 1,
                                               'flatpak': {'base_image': base_image}})
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
        with open(workflow.builder.df_path) as f:
            df = f.read()

        assert "FROM " + base_image in df
        assert 'name="{}"'.format(config['name']) in df
        assert 'com.redhat.component="{}"'.format(config['component']) in df

        m = re.search(r'module enable\s*(.*?)\s*&&', df)
        assert m
        enabled_modules = sorted(m.group(1).split())

        if config_name == 'app':
            assert enabled_modules == ['eog:f28', 'flatpak-runtime:f28']
        else:
            assert enabled_modules == ['flatpak-runtime:f28']

        includepkgs_path = os.path.join(workflow.builder.df_dir, 'atomic-reactor-includepkgs')
        assert os.path.exists(includepkgs_path)
        with open(includepkgs_path) as f:
            includepkgs = f.read()
            assert 'librsvg2' in includepkgs
            if config_name == 'app':
                assert 'eog-0:3.24.1-1.module_7b96ed10.x86_64' in includepkgs

        assert os.path.exists(os.path.join(workflow.builder.df_dir, 'cleanup.sh'))
