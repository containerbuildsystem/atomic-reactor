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
import yaml

from atomic_reactor.inner import DockerBuildWorkflow
try:
    from atomic_reactor.plugins.pre_flatpak_create_dockerfile import (get_flatpak_source_spec,
                                                                      FlatpakCreateDockerfilePlugin)
except ImportError:
    pass

from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
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
        self.image_id = "xxx"
        self.base_image = ImageName.parse("org.gnome.eog")
        self.df_path = None
        self.dockerfile_images = None

    def set_base_image(self, base_image):
        pass

    def set_df_path(self, path):
        self.df_path = path
        self.dockerfile_images = DockerfileImages([])


def mock_workflow(tmpdir, container_yaml, user_params=None):
    if user_params is None:
        user_params = USER_PARAMS
    workflow = DockerBuildWorkflow(source=None)
    mock_source = MockSource(tmpdir)
    setattr(workflow, 'builder', MockBuilder())
    flexmock(workflow, source=mock_source)
    workflow.user_params = user_params

    with open(mock_source.container_yaml_path, "w") as f:
        f.write(container_yaml)
    workflow.source.config = SourceConfig(str(tmpdir))
    workflow.df_dir = str(tmpdir)

    return workflow


CONFIGS = build_flatpak_test_configs()


@responses.activate  # noqa - docker_tasker fixture
@pytest.mark.skipif(not MODULEMD_AVAILABLE,
                    reason='libmodulemd not available')
@pytest.mark.parametrize('config_name,override_base_image,breakage', [
    ('app', None, None),
    ('app', 'registry.fedoraproject.org/fedora:29', None),
    ('runtime', None, None),
    ('app', None, 'no_modules'),
    ('app', None, 'multiple_modules'),
])
def test_flatpak_create_dockerfile(tmpdir, docker_tasker, user_params,
                                   config_name, override_base_image, breakage):
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

    workflow = mock_workflow(tmpdir, container_yaml)

    source_spec = get_flatpak_source_spec(workflow)
    assert source_spec is None

    base_image = "registry.fedoraproject.org/fedora:latest"

    workflow.conf.conf = {'version': 1, 'flatpak': {'base_image': base_image},
                          'source_registry': {'url': 'source_registry'}}

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FlatpakCreateDockerfilePlugin.key,
            'args': {}
        }]
    )

    if expected_exception:
        with pytest.raises(PluginFailedException) as ex:
            runner.run()
        assert expected_exception in str(ex.value)
    else:
        runner.run()

        assert os.path.exists(workflow.df_path)
        with open(workflow.df_path) as f:
            df = f.read()

        expect_base_image = override_base_image if override_base_image else base_image
        assert "FROM " + expect_base_image in df
        assert 'name="{}"'.format(config['name']) in df
        assert 'com.redhat.component="{}"'.format(config['component']) in df
        assert "RUN rm -f /etc/yum.repos.d/*" in df
        assert "ADD atomic-reactor-repos/* /etc/yum.repos.d/" in df

        source_spec = get_flatpak_source_spec(workflow)
        assert source_spec == config['source_spec']


def test_skip_plugin(tmpdir, caplog, docker_tasker, user_params):
    workflow = mock_workflow(tmpdir, "", user_params={})

    base_image = "registry.fedoraproject.org/fedora:latest"

    args = {
        'base_image': base_image,
    }

    workflow.conf.conf = {'version': 1, 'flatpak': {'base_image': base_image}}

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FlatpakCreateDockerfilePlugin.key,
            'args': args
        }]
    )

    runner.run()

    assert 'not flatpak build, skipping plugin' in caplog.text
