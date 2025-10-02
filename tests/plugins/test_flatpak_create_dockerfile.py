"""
Copyright (c) 2017-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from pathlib import Path
from flexmock import flexmock

import responses
import os
import pytest
import yaml
from typing import List

from atomic_reactor.dirs import BuildDir
from atomic_reactor.utils.flatpak_util import FlatpakUtil
from tests.mock_env import MockEnv

try:
    from atomic_reactor.plugins.flatpak_create_dockerfile import FlatpakCreateDockerfilePlugin
except ImportError:
    pass

from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.source import VcsInfo, SourceConfig
from atomic_reactor.util import DockerfileImages
from osbs.utils import ImageName

from tests.constants import FLATPAK_GIT, FLATPAK_SHA1
from tests.flatpak import MODULEMD_AVAILABLE, build_flatpak_test_configs


USER_PARAMS = {'flatpak': True}


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
        self.base_image = ImageName.parse("org.gnome.eog")
        self.df_path = None
        self.dockerfile_images = None

    def set_base_image(self, base_image):
        pass

    def set_df_path(self, path):
        self.df_path = path
        self.dockerfile_images = DockerfileImages([])


def mock_workflow(
    workflow, source_dir: Path, container_yaml, platforms: List[str], user_params=None
):
    mock_source = MockSource(str(source_dir))
    setattr(workflow, 'builder', MockBuilder())
    flexmock(workflow, source=mock_source)

    if user_params is None:
        workflow.user_params.update(USER_PARAMS)

    with open(mock_source.container_yaml_path, "w") as f:
        f.write(container_yaml)
    workflow.source.config = SourceConfig(str(source_dir))
    workflow.build_dir.init_build_dirs(platforms, workflow.source)

    return workflow


CONFIGS = build_flatpak_test_configs()


@responses.activate  # noqa
@pytest.mark.skipif(not MODULEMD_AVAILABLE,
                    reason='libmodulemd not available')
@pytest.mark.parametrize('config_name,override_base_image,breakage', [
    ('app', None, None),
    ('app', 'registry.fedoraproject.org/fedora:29', None),
    ('runtime', None, None),
    ('app', None, 'no_modules'),
    ('app', None, 'multiple_modules'),
])
def test_flatpak_create_dockerfile(workflow, source_dir, config_name, override_base_image,
                                   breakage):
    config = CONFIGS[config_name]

    modules = None
    if breakage == 'no_modules':
        modules = []
        expected_exception = "a module is required for Flatpaks"
    elif breakage == 'multiple_modules':
        modules = ['eog:f28:20170629213428', 'flatpak-common:f28:123456']
        expected_exception = None  # Just a warning
    else:
        assert breakage is None
        expected_exception = None

    data = yaml.safe_load(config['container_yaml'])
    if override_base_image is not None:
        data['flatpak']['base_image'] = override_base_image
    if modules is not None:
        data['compose']['modules'] = modules
    container_yaml = yaml.dump(data)

    platforms = ["x86_64", "s390x"]
    mock_workflow(workflow, source_dir, container_yaml, platforms)

    base_image = "registry.fedoraproject.org/fedora:latest"

    reactor_config = {
        'version': 1,
        'flatpak': {'base_image': base_image},
        'source_registry': {'url': 'source_registry'},
    }

    runner = (MockEnv(workflow)
              .for_plugin(FlatpakCreateDockerfilePlugin.key)
              .set_reactor_config(reactor_config)
              .create_runner())

    if expected_exception:
        with pytest.raises(PluginFailedException) as ex:
            runner.run()
        assert expected_exception in str(ex.value)
    else:
        runner.run()

        flatpak_util = FlatpakUtil(workflow_config=None, source_config=workflow.source.config)
        source_spec = flatpak_util.get_flatpak_source_spec()
        assert source_spec == config['source_spec']

        expect_base_image = override_base_image if override_base_image else base_image

        for platform in platforms:
            build_dir = BuildDir(workflow.build_dir.path / platform, platform)
            df = build_dir.dockerfile_path.read_text("utf-8")

            assert "FROM " + expect_base_image in df
            assert 'name="{}"'.format(config['name']) in df
            assert 'com.redhat.component="{}"'.format(config['component']) in df
            assert "RUN rm -f /etc/yum.repos.d/*" in df
            assert "COPY atomic-reactor-repos/* /etc/yum.repos.d/" in df


def test_skip_plugin(workflow, source_dir, caplog):
    mock_workflow(workflow, source_dir, "", ["x86_64"], user_params={})
    base_image = "registry.fedoraproject.org/fedora:latest"
    (MockEnv(workflow)
     .for_plugin(FlatpakCreateDockerfilePlugin.key)
     .set_plugin_args({'base_image': base_image})
     .set_reactor_config({'flatpak': {'base_image': base_image}})
     .create_runner()
     .run())
    assert 'not flatpak build, skipping plugin' in caplog.text
