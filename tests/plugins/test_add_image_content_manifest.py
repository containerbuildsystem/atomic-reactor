"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import json
import os
import sys

from copy import deepcopy
from textwrap import dedent

import pytest
import yaml

from tests.mock_env import MockEnv
from tests.utils.test_cachito import CACHITO_URL, CACHITO_REQUEST_ID

from atomic_reactor import util
from atomic_reactor.constants import INSPECT_ROOTFS, INSPECT_ROOTFS_LAYERS
from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.pre_add_image_content_manifest import AddImageContentManifestPlugin

pytestmark = pytest.mark.usefixtures('user_params')

CONTENT_SETS = {
    'x86_64': ['pulp-spamx86-rpms', 'pulp-baconx86-rpms'],
    'ppc64': ['pulp-spamppc64-rpms', 'pulp-baconppc64-rpms'],
    's390x': ['pulp-spams390x-rpms', 'pulp-bacons390x-rpms'],
}
CACHITO_ICM_URL = '{}/api/v1/requests/{}/content-manifest'.format(CACHITO_URL,
                                                                  CACHITO_REQUEST_ID)
ICM_MINIMAL_DICT = {
    'metadata': {
        'icm_version': 1,
        'icm_spec': ('https://raw.githubusercontent.com/containerbuildsystem/atomic-reactor/'
                     'master/atomic_reactor/schemas/content_manifest.json'),
        'image_layer_index': 1
    },
    'content_sets': [],
    'image_contents': [],
}
ICM_DICT = {
    'metadata': {
        'icm_version': 1,
        'icm_spec': 'https://link.to.icm.specification',
        'image_layer_index': 1,
    },
    'content_sets': [],
    'image_contents': [
        {
            'purl':
            'pkg:golang/github.com%2Frelease-engineering%2Fretrodep%2Fv2@v2.0.2',
            'dependencies': [
                {
                    'purl':
                    'pkg:golang/github.com%2Fop%2Fgo-logging@v0.0.0'
                },
            ],
            'sources': [
                {
                    'purl':
                    'pkg:golang/github.com%2FMasterminds%2Fsemver@v1.4.2'
                },
            ]
        },
    ]
}
ICM_JSON = dedent(
    '''\
    {
        "metadata": {
        "icm_version": 1,
        "icm_spec": "https://link.to.icm.specification",
        "image_layer_index": 1
        },
        "content_sets": [],
        "image_contents": [
        {
            "purl": "pkg:golang/github.com%2Frelease-engineering%2Fretrodep%2Fv2@v2.0.2",
            "dependencies": [
            {
                "purl": "pkg:golang/github.com%2Fop%2Fgo-logging@v0.0.0"
            }
            ],
            "sources": [
            {
                "purl": "pkg:golang/github.com%2FMasterminds%2Fsemver@v1.4.2"
            }
            ]
        }
        ]
    }
    '''
)


def setup_function(*args):
    # IMPORTANT: This needs to be done to ensure mocks at the module
    # level are reset between test cases.
    sys.modules.pop('pre_add_image_content_manifest', None)


def teardown_function(*args):
    # IMPORTANT: This needs to be done to ensure mocks at the module
    # level are reset between test cases.
    sys.modules.pop('pre_add_image_content_manifest', None)


def mock_content_sets_config(tmpdir, empty=False):
    content_dict = {}
    if not empty:
        for arch, repos in CONTENT_SETS.items():
            content_dict[arch] = repos
    tmpdir.join('content_sets.yml').write(yaml.safe_dump(content_dict))


def mock_get_icm(requests_mock):
    requests_mock.register_uri('GET', CACHITO_ICM_URL, text=ICM_JSON)


def mock_env(tmpdir, docker_tasker, platform='x86_64', base_layers=0,
             icm_url=CACHITO_ICM_URL, r_c_m_override=None,
             ):
    inspection_data = {
        INSPECT_ROOTFS: {
            INSPECT_ROOTFS_LAYERS: list(range(base_layers))
        }
    }
    if r_c_m_override is None:
        r_c_m = {
            'version': 1,
            'cachito': {
                'api_url': CACHITO_URL,
                'auth': {
                    'ssl_certs_dir': str(tmpdir),
                },
            },
        }
    else:
        r_c_m = r_c_m_override
    env = (MockEnv()
           .for_plugin('prebuild', AddImageContentManifestPlugin.key,
                       {'remote_source_icm_url': icm_url})
           .set_reactor_config(r_c_m)
           .make_orchestrator()
           )
    tmpdir.join('cert').write('')
    env.workflow.builder.set_inspection_data(inspection_data)
    env.workflow.user_params['platform'] = platform

    # Ensure to succeed in reading the content_sets.yml
    env.workflow.source.get_build_file_path = lambda: (str(tmpdir), str(tmpdir))

    return env.create_runner(docker_tasker)


@pytest.mark.parametrize('manifest_file_exists', [True, False])
@pytest.mark.parametrize('content_sets', [True, False])
@pytest.mark.parametrize('platform', ['x86_64', 'ppc64', 's390x'])
@pytest.mark.parametrize(
    ('df_content, expected_df, base_layers, manifest_file'), [
        (
            dedent("""\
            FROM base_image
            CMD build /spam/eggs
            LABEL com.redhat.component=eggs version=1.0 release=42
        """),
            dedent("""\
            FROM base_image
            CMD build /spam/eggs
            ADD eggs-1.0-42.json /root/buildinfo/content_manifests/eggs-1.0-42.json
            LABEL com.redhat.component=eggs version=1.0 release=42
        """),
            2,
            'eggs-1.0-42.json',
        ),
        (
            dedent("""\
            FROM base_image
            CMD build /spam/eggs
            LABEL com.redhat.component=eggs version=1.0 release=42
        """),
            dedent("""\
            FROM base_image
            CMD build /spam/eggs
            ADD eggs-1.0-42.json /root/buildinfo/content_manifests/eggs-1.0-42.json
            LABEL com.redhat.component=eggs version=1.0 release=42
        """),
            3,
            'eggs-1.0-42.json',
        ),
        (
            dedent("""\
            FROM scratch
            CMD build /spam/eggs
            LABEL com.redhat.component=eggs version=1.0 release=42
        """),
            dedent("""\
            FROM scratch
            CMD build /spam/eggs
            ADD eggs-1.0-42.json /root/buildinfo/content_manifests/eggs-1.0-42.json
            LABEL com.redhat.component=eggs version=1.0 release=42
        """),
            0,
            'eggs-1.0-42.json',
        ),
    ])
def test_add_image_content_manifest(requests_mock, tmpdir, docker_tasker, caplog,
                                    manifest_file_exists, content_sets, platform,
                                    df_content, expected_df, base_layers, manifest_file,
                                    ):
    mock_get_icm(requests_mock)
    mock_content_sets_config(tmpdir, empty=(not content_sets))
    dfp = util.df_parser(str(tmpdir))
    dfp.content = df_content
    if manifest_file_exists:
        tmpdir.join(manifest_file).write("")
    runner = mock_env(tmpdir, docker_tasker, platform, base_layers)
    runner.workflow.builder.set_df_path(dfp.dockerfile_path)
    if manifest_file_exists:
        with pytest.raises(PluginFailedException):
            runner.run()
        log_msg = 'File {} already exists in repo'.format(os.path.join(str(tmpdir), manifest_file))
        assert log_msg in caplog.text
        return
    expected_output = deepcopy(ICM_DICT)
    if content_sets:
        expected_output['content_sets'] = CONTENT_SETS[platform]
    expected_output['metadata']['image_layer_index'] = base_layers if base_layers else 1
    runner.run()
    assert dfp.content == expected_df
    output_file = os.path.join(str(tmpdir), manifest_file)
    with open(output_file) as f:
        json_data = f.read()
    output = json.loads(json_data)
    assert expected_output == output


@pytest.mark.parametrize('content_sets', [True, False])
@pytest.mark.parametrize(
    ('df_content, base_layers, manifest_file'), [
        (
            dedent("""\
            FROM base_image
            CMD build /spam/eggs
            LABEL com.redhat.component=eggs version=1.0 release=42
        """),
            2,
            'eggs-1.0-42.json',
        )])
def test_none_remote_source_icm_url(requests_mock, tmpdir, docker_tasker, caplog,
                                    content_sets,
                                    df_content, base_layers, manifest_file,
                                    ):
    platform = 'x86_64'
    mock_get_icm(requests_mock)
    mock_content_sets_config(tmpdir, empty=(not content_sets))
    dfp = util.df_parser(str(tmpdir))
    dfp.content = df_content
    runner = mock_env(tmpdir, docker_tasker, platform, base_layers, icm_url=None)
    runner.workflow.builder.set_df_path(dfp.dockerfile_path)
    expected_output = deepcopy(ICM_MINIMAL_DICT)
    if content_sets:
        expected_output['content_sets'] = CONTENT_SETS[platform]
    expected_output['metadata']['image_layer_index'] = base_layers
    runner.run()
    output_file = os.path.join(str(tmpdir), manifest_file)
    with open(output_file) as f:
        json_data = f.read()
    output = json.loads(json_data)
    assert expected_output == output


def test_missing_cachito_conf(requests_mock, tmpdir, docker_tasker, caplog,):
    df_content = dedent("""\
            FROM base_image
            CMD build /spam/eggs
            LABEL com.redhat.component=eggs version=1.0 release=42
        """)

    # No 'cachito' conf in the reactor-config
    r_c_m = {
        'version': 1,
    }
    base_layers = 0
    manifest_file = 'eggs-1.0-42.json'
    mock_get_icm(requests_mock)
    dfp = util.df_parser(str(tmpdir))
    dfp.content = df_content
    runner = mock_env(tmpdir, docker_tasker, r_c_m_override=r_c_m)
    runner.workflow.builder.set_df_path(dfp.dockerfile_path)
    expected_output = deepcopy(ICM_DICT)
    expected_output['metadata']['image_layer_index'] = base_layers
    runner.run()
    output_file = os.path.join(str(tmpdir), manifest_file)
    with open(output_file) as f:
        json_data = f.read()
    output = json.loads(json_data)
    assert expected_output == output
