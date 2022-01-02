"""
Copyright (c) 2017-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from pathlib import Path

from flexmock import flexmock

from copy import deepcopy
import koji
import responses
import os
import pytest
import re

try:
    from atomic_reactor.plugins.pre_flatpak_create_dockerfile import set_flatpak_source_spec
    from atomic_reactor.plugins.pre_flatpak_update_dockerfile import (FlatpakUpdateDockerfilePlugin,
                                                                      get_flatpak_compose_info,
                                                                      get_flatpak_source_info)
except ImportError:
    pass

from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.source import VcsInfo, SourceConfig
from osbs.utils import ImageName

from tests.constants import (FLATPAK_GIT, FLATPAK_SHA1)
from tests.flatpak import MODULEMD_AVAILABLE, build_flatpak_test_configs, setup_flatpak_composes


DF_CONTENT = """FROM fedora:latest
LABEL release="@RELEASE@"
RUN $DNF module enable @ENABLE_MODULES@
RUN $DNF install @INSTALL_PACKAGES@
CMD sleep 1000
"""

ODCS_URL = 'https://odcs.fedoraproject.org/odcs/1'

CONFIGS = build_flatpak_test_configs()

USER_PARAMS = {'flatpak': True}

pytestmark = pytest.mark.usefixtures('user_params')


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


def mock_workflow(workflow, build_dir: Path, container_yaml, user_params=None, platforms=None):
    if user_params is None:
        user_params = USER_PARAMS
    mock_source = MockSource(str(build_dir))
    flexmock(workflow, source=mock_source)

    with open(mock_source.container_yaml_path, "w") as f:
        f.write(container_yaml)
    workflow.source.config = SourceConfig(str(build_dir))
    workflow.user_params = user_params
    if not platforms:
        platforms = ['x86_64']
    Path(workflow.source.path, "Dockerfile").write_text(DF_CONTENT)
    workflow.build_dir.init_build_dirs(platforms, workflow.source)

    return workflow


def mock_koji_session(config):
    session = flexmock()

    (session
     .should_receive('krb_login')
     .and_return(True))

    for module_name, info in config['modules'].items():
        build = {
            'build_id': info['build_id'],
            'name': module_name,
            'version': info['stream'],
            'release': info['version'] + "." + info['context'],
            'extra': {
                'typeinfo': {
                    'module': {
                        'modulemd_str': info['metadata']
                    }
                }
            }
        }

        (session
         .should_receive('getPackageID')
         .with_args(module_name)
         .and_return(info['package_id']))

        (session
         .should_receive('getBuild')
         .with_args(module_name + '-' + info['stream'] + info['version'] + "." + info['context'])
         .and_return(build))

        (session
         .should_receive('listBuilds')
         .with_args(state=1, type="module", packageID=info['package_id'])
         .and_return([build]))

        (session
         .should_receive('listArchives')
         .with_args(buildID=info['build_id'])
         .and_return(
             [{'btype': 'module',
               'build_id': info['build_id'],
               'filename': 'modulemd.txt',
               'id': info['archive_id']}]))

        (session
         .should_receive('listRPMs')
         .with_args(imageID=info['archive_id'])
         .and_return(info['koji_rpms']))

    (flexmock(koji)
        .should_receive('ClientSession')
        .and_return(session))


@responses.activate  # noqa
@pytest.mark.skipif(not MODULEMD_AVAILABLE,
                    reason='libmodulemd not available')
@pytest.mark.parametrize('config_name,breakage', [
    ('app', None),
    ('app', 'no_compose'),
    ('runtime', None),
    ('runtime', 'branch_mismatch'),
])
def test_flatpak_update_dockerfile(workflow, build_dir, config_name, breakage):
    config = CONFIGS[config_name]

    container_yaml = config['container_yaml']

    workflow = mock_workflow(workflow, build_dir, container_yaml)

    assert get_flatpak_compose_info(workflow) is None
    assert get_flatpak_source_info(workflow) is None

    if breakage == 'branch_mismatch':
        config = deepcopy(config)
        base_module = config['modules'][config['base_module']]
        base_module['metadata'] = base_module['metadata'].replace('branch: f28',
                                                                  'branch: MISMATCH')

        expected_exception = "Mismatch for 'branch'"
    elif breakage == 'no_compose':
        config = deepcopy(config)
        config['odcs_composes'] = []
        expected_exception = "Can't find main module"
    else:
        assert breakage is None
        expected_exception = None

    mock_koji_session(config)

    set_flatpak_source_spec(workflow, config['source_spec'])

    # composes run by resolve_composes plugin
    setup_flatpak_composes(workflow, config)

    secrets_path = build_dir / "secret"
    secrets_path.mkdir()
    secrets_path.joinpath("token").write_text("green_eggs_and_ham", "utf-8")

    rcm = {'version': 1,
           'odcs': {'api_url': ODCS_URL,
                    'auth': {'openidc_dir': secrets_path},
                    'signing_intents': [
                        {
                            'name': 'unsigned',
                            'keys': [],
                        },
                        {
                            'name': 'release',
                            'keys': ['R123', 'R234'],
                        },
                        {
                            'name': 'beta',
                            'keys': ['R123', 'B456', 'B457'],
                        },
                    ],
                    'default_signing_intent': 'unsigned'},
           'koji': {'auth': {},
                    'hub_url': 'https://koji.example.com/hub'}}
    workflow.conf.conf = rcm

    runner = PreBuildPluginsRunner(
        workflow,
        [{
            'name': FlatpakUpdateDockerfilePlugin.key,
        }]
    )

    if expected_exception:
        with pytest.raises(PluginFailedException) as ex:
            runner.run()
        assert expected_exception in str(ex.value)
    else:
        runner.run()

        assert os.path.exists(workflow.build_dir.any_platform.dockerfile_path)
        df = workflow.build_dir.any_platform.dockerfile.content

        m = re.search(r'module enable\s*(.*?)\s*$', df, re.MULTILINE)
        assert m
        enabled_modules = sorted(m.group(1).split())

        if config_name == 'app':
            assert enabled_modules == ['eog:f28', 'flatpak-runtime:f28']
        else:
            assert enabled_modules == ['flatpak-runtime:f28']

        includepkgs_path = os.path.join(workflow.build_dir.any_platform.path,
                                        'atomic-reactor-includepkgs')
        assert os.path.exists(includepkgs_path)
        with open(includepkgs_path) as f:
            includepkgs = f.read()
            assert 'librsvg2' in includepkgs
            if config_name == 'app':
                assert 'eog-0:3.28.3-1.module_2123+73a9ef6f.x86_64' in includepkgs

        assert os.path.exists(os.path.join(workflow.build_dir.any_platform.path, 'cleanup.sh'))

        compose_info = get_flatpak_compose_info(workflow)
        assert compose_info.source_spec == config['source_spec']

        if config_name == 'app':
            assert compose_info.main_module.name == 'eog'
            assert compose_info.main_module.stream == 'f28'
            assert compose_info.main_module.version == '20170629213428'
            assert (compose_info.main_module.mmd.get_summary("C") ==
                    'Eye of GNOME Application Module')
            assert compose_info.main_module.rpms == [
                'eog-0:3.28.3-1.module_2123+73a9ef6f.src.rpm',
                'eog-0:3.28.3-1.module_2123+73a9ef6f.x86_64.rpm',
                'eog-0:3.28.3-1.module_2123+73a9ef6f.ppc64le.rpm',
            ]

        source_info = get_flatpak_source_info(workflow)
        assert source_info.base_module.name == config['base_module']


@pytest.mark.skipif(not MODULEMD_AVAILABLE,
                    reason='libmodulemd not available')
def test_skip_plugin(workflow, build_dir, caplog):
    workflow = mock_workflow(workflow, build_dir, "", user_params={})

    runner = PreBuildPluginsRunner(
        workflow,
        [{
            'name': FlatpakUpdateDockerfilePlugin.key,
            'args': {}
        }]
    )

    runner.run()

    assert 'not flatpak build, skipping plugin' in caplog.text
