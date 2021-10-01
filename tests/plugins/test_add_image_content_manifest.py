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
from flexmock import flexmock

from tests.mock_env import MockEnv
from tests.utils.test_cachito import CACHITO_URL, CACHITO_REQUEST_ID

from atomic_reactor import util
from atomic_reactor.utils import imageutil
from atomic_reactor.constants import INSPECT_ROOTFS, INSPECT_ROOTFS_LAYERS, PLUGIN_FETCH_MAVEN_KEY
from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.pre_add_image_content_manifest import AddImageContentManifestPlugin

pytestmark = pytest.mark.usefixtures('user_params')

CONTENT_SETS = {
    'x86_64': ['pulp-spamx86-rpms', 'pulp-baconx86-rpms'],
    'ppc64': ['pulp-spamppc64-rpms', 'pulp-baconppc64-rpms'],
    's390x': ['pulp-spams390x-rpms', 'pulp-bacons390x-rpms'],
}
CACHITO_ICM_URL = '{}/api/v1/content-manifest?requests={}'.format(CACHITO_URL,
                                                                  CACHITO_REQUEST_ID)
PNC_ARTIFACT = {
            'id': 1234,
            'publicUrl': 'http://test.com/artifact.jar',
            'md5': 'abcd',
            'sha1': 'abcd',
            'sha256': 'abcd',
            'purl': 'pkg:maven/org.example.artifact/artifact-common@0.0.1.redhat-00001?type=jar',
        }
PNC_ROOT = 'http://pnc.example.com/pnc-rest/v2'
PNC_ARTIFACT_URL = f"{PNC_ROOT}/artifacts/{PNC_ARTIFACT['id']}"
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
        {
            'purl': PNC_ARTIFACT['purl'],
        }
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

REMOTE_SOURCES = [{
    'build_args': None,
    'configs': None,
    'request_id': CACHITO_REQUEST_ID,
    'url': None,
    'name': None,
}]


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
    requests_mock.register_uri('GET', PNC_ARTIFACT_URL, text=json.dumps(PNC_ARTIFACT))


def mock_env(tmpdir, platform='x86_64', base_layers=0,
             remote_sources=REMOTE_SOURCES, r_c_m_override=None, pnc_artifacts=True,
             ):  # pylint: disable=W0102
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
            'pnc': {
                'base_api_url': PNC_ROOT,
                'get_artifact_path': 'artifacts/{}',
            },
        }
    else:
        r_c_m = r_c_m_override

    env = (MockEnv()
           .for_plugin('prebuild', AddImageContentManifestPlugin.key,
                       {'remote_sources': remote_sources})
           .set_reactor_config(r_c_m)
           .make_orchestrator()
           )
    if pnc_artifacts:
        env.set_plugin_result(
            'prebuild', PLUGIN_FETCH_MAVEN_KEY, {'pnc_artifact_ids': [PNC_ARTIFACT['id']]}
        )
    tmpdir.join('cert').write('')
    flexmock(imageutil).should_receive('base_image_inspect').and_return(inspection_data)
    env.workflow.user_params['platform'] = platform

    # Ensure to succeed in reading the content_sets.yml
    env.workflow.source.get_build_file_path = lambda: (str(tmpdir), str(tmpdir))

    return env.create_runner()


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
def test_add_image_content_manifest(requests_mock, tmpdir, caplog,
                                    manifest_file_exists, content_sets, platform,
                                    df_content, expected_df, base_layers, manifest_file,
                                    ):
    mock_get_icm(requests_mock)
    mock_content_sets_config(tmpdir, empty=(not content_sets))
    dfp = util.df_parser(str(tmpdir))
    dfp.content = df_content
    if manifest_file_exists:
        tmpdir.join(manifest_file).write("")
    runner = mock_env(tmpdir, platform, base_layers)
    runner.workflow.set_df_path(dfp.dockerfile_path)
    runner.workflow.df_dir = str(tmpdir)
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


def test_fetch_maven_artifacts_no_pnc_config(requests_mock, tmpdir, caplog, user_params):
    mock_get_icm(requests_mock)
    dfp = util.df_parser(str(tmpdir))
    dfp.content = dedent("""\
                            FROM base_image
                            CMD build /spam/eggs
                            LABEL com.redhat.component=eggs version=1.0 release=42
                        """)
    r_c_m = {
        'version': 1,
        'cachito': {
            'api_url': CACHITO_URL,
            'auth': {
                'ssl_certs_dir': str(tmpdir),
            },
        },
    }

    with pytest.raises(PluginFailedException):
        runner = mock_env(tmpdir, r_c_m_override=r_c_m)
        runner.workflow.set_df_path(dfp.dockerfile_path)
        runner.run()

    msg = 'No PNC configuration found in reactor config map'
    assert msg in caplog.text


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
def test_none_remote_source_icm_url(requests_mock, tmpdir, caplog, content_sets, df_content,
                                    base_layers, manifest_file):
    platform = 'x86_64'
    mock_get_icm(requests_mock)
    mock_content_sets_config(tmpdir, empty=(not content_sets))
    dfp = util.df_parser(str(tmpdir))
    dfp.content = df_content
    runner = mock_env(tmpdir, platform, base_layers, remote_sources=None)
    runner.workflow.set_df_path(dfp.dockerfile_path)
    runner.workflow.df_dir = str(tmpdir)
    expected_output = deepcopy(ICM_MINIMAL_DICT)
    expected_output['image_contents'].append({'purl': PNC_ARTIFACT['purl']})
    if content_sets:
        expected_output['content_sets'] = CONTENT_SETS[platform]
    expected_output['metadata']['image_layer_index'] = base_layers
    runner.run()
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
def test_no_pnc_artifacts(requests_mock, tmpdir, caplog, content_sets, df_content,
                          base_layers, manifest_file):
    platform = 'x86_64'
    mock_get_icm(requests_mock)
    mock_content_sets_config(tmpdir, empty=(not content_sets))
    dfp = util.df_parser(str(tmpdir))
    dfp.content = df_content
    runner = mock_env(tmpdir, platform, base_layers, pnc_artifacts=False)
    runner.workflow.set_df_path(dfp.dockerfile_path)
    runner.workflow.df_dir = str(tmpdir)
    expected_output = deepcopy(ICM_DICT)
    expected_output['image_contents'] = expected_output['image_contents'][:-1]
    if content_sets:
        expected_output['content_sets'] = CONTENT_SETS[platform]
    expected_output['metadata']['image_layer_index'] = base_layers
    runner.run()
    output_file = os.path.join(str(tmpdir), manifest_file)
    with open(output_file) as f:
        json_data = f.read()
    output = json.loads(json_data)
    assert expected_output == output
