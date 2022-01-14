"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import json
import sys
from pathlib import Path

from copy import deepcopy
from textwrap import dedent

import pytest
import yaml
from flexmock import flexmock

from tests.mock_env import MockEnv
from tests.utils.test_cachito import CACHITO_URL, CACHITO_REQUEST_ID

from atomic_reactor.constants import (
    INSPECT_ROOTFS,
    INSPECT_ROOTFS_LAYERS,
    PLUGIN_FETCH_MAVEN_KEY,
    PLUGIN_RESOLVE_REMOTE_SOURCE,
)
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
    'id': CACHITO_REQUEST_ID,
    # 'url': 'some url',
    # 'name': None,
    # 'remote_source_json': {},
    # 'remote_source_tarball': {},
}]


def setup_function(*args):
    # IMPORTANT: This needs to be done to ensure mocks at the module
    # level are reset between test cases.
    sys.modules.pop('pre_add_image_content_manifest', None)


def teardown_function(*args):
    # IMPORTANT: This needs to be done to ensure mocks at the module
    # level are reset between test cases.
    sys.modules.pop('pre_add_image_content_manifest', None)


def mock_content_sets_config(source_path: str, empty=False):
    content_dict = {}
    if not empty:
        for arch, repos in CONTENT_SETS.items():
            content_dict[arch] = repos
    Path(source_path, 'content_sets.yml').write_text(yaml.safe_dump(content_dict))


def mock_get_icm(requests_mock):
    requests_mock.register_uri('GET', CACHITO_ICM_URL, text=ICM_JSON)
    requests_mock.register_uri('GET', PNC_ARTIFACT_URL, text=json.dumps(PNC_ARTIFACT))


def mock_env(workflow, df_content, base_layers=0, remote_sources=None,
             r_c_m_override=None, pnc_artifacts=True):

    if base_layers > 0:
        inspection_data = {
            INSPECT_ROOTFS: {
                INSPECT_ROOTFS_LAYERS: list(range(base_layers))
            }
        }
    else:
        inspection_data = {}

    certs_dir = Path(workflow.source.path)
    certs_dir.joinpath('cert').write_text('')

    if r_c_m_override is None:
        r_c_m = {
            'version': 1,
            'cachito': {
                'api_url': CACHITO_URL,
                'auth': {
                    'ssl_certs_dir': str(certs_dir),
                },
            },
            'pnc': {
                'base_api_url': PNC_ROOT,
                'get_artifact_path': 'artifacts/{}',
            },
        }
    else:
        r_c_m = r_c_m_override

    env = (MockEnv(workflow)
           .for_plugin('prebuild', AddImageContentManifestPlugin.key)
           .set_reactor_config(r_c_m)
           .set_plugin_result('prebuild', PLUGIN_RESOLVE_REMOTE_SOURCE, remote_sources)
           .make_orchestrator()
           )
    if pnc_artifacts:
        env.set_plugin_result(
            'prebuild', PLUGIN_FETCH_MAVEN_KEY, {'pnc_artifact_ids': [PNC_ARTIFACT['id']]}
        )
    flexmock(workflow.imageutil).should_receive('base_image_inspect').and_return(inspection_data)

    Path(workflow.source.path, "Dockerfile").write_text(df_content)

    platforms = list(CONTENT_SETS.keys())
    workflow.build_dir.init_build_dirs(platforms, workflow.source)

    return env.create_runner()


def check_icm(expect_filename: str, platform_independent_data: dict, has_content_sets: bool):
    """Make a function that checks the expected ICM in a build dir."""

    def check_in_build_dir(build_dir):
        expect_data = deepcopy(platform_independent_data)
        if has_content_sets:
            expect_data['content_sets'] = CONTENT_SETS[build_dir.platform]

        icm_content = build_dir.path.joinpath(expect_filename).read_text()
        actual_data = json.loads(icm_content)

        assert actual_data == expect_data

    return check_in_build_dir


@pytest.mark.parametrize('manifest_file_exists', [True, False])
@pytest.mark.parametrize('content_sets', [True, False])
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
def test_add_image_content_manifest(workflow, requests_mock,
                                    manifest_file_exists, content_sets,
                                    df_content, expected_df, base_layers, manifest_file,
                                    ):
    mock_get_icm(requests_mock)
    mock_content_sets_config(workflow.source.path, empty=(not content_sets))

    runner = mock_env(workflow, df_content, base_layers, remote_sources=REMOTE_SOURCES)

    if manifest_file_exists:
        workflow.build_dir.any_platform.path.joinpath(manifest_file).touch()
        err_msg = f'File .*/{manifest_file} already exists in repo'

        with pytest.raises(PluginFailedException, match=err_msg):
            runner.run()
        return

    expected_output = deepcopy(ICM_DICT)
    expected_output['metadata']['image_layer_index'] = base_layers if base_layers else 0

    runner.run()

    workflow.build_dir.for_each_platform(
        check_icm(manifest_file, expected_output, has_content_sets=content_sets)
    )

    def check_df(build_dir):
        assert build_dir.dockerfile_path.read_text() == expected_df

    workflow.build_dir.for_each_platform(check_df)


def test_fetch_maven_artifacts_no_pnc_config(workflow, requests_mock, caplog):
    mock_get_icm(requests_mock)
    df_content = dedent("""\
                            FROM base_image
                            CMD build /spam/eggs
                            LABEL com.redhat.component=eggs version=1.0 release=42
                        """)
    r_c_m = {
        'version': 1,
        'cachito': {
            'api_url': CACHITO_URL,
            'auth': {
                'ssl_certs_dir': workflow.source.path,
            },
        },
    }

    with pytest.raises(PluginFailedException):
        runner = mock_env(workflow, df_content, r_c_m_override=r_c_m, remote_sources=REMOTE_SOURCES)
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
def test_none_remote_source_icm_url(workflow, requests_mock,
                                    content_sets, df_content, base_layers, manifest_file):
    mock_get_icm(requests_mock)
    mock_content_sets_config(workflow.source.path, empty=(not content_sets))

    runner = mock_env(workflow, df_content, base_layers)
    runner.run()

    expected_output = deepcopy(ICM_MINIMAL_DICT)
    expected_output['image_contents'].append({'purl': PNC_ARTIFACT['purl']})
    expected_output['metadata']['image_layer_index'] = base_layers

    workflow.build_dir.for_each_platform(
        check_icm(manifest_file, expected_output, has_content_sets=content_sets)
    )


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
def test_no_pnc_artifacts(workflow, requests_mock, content_sets, df_content,
                          base_layers, manifest_file):
    mock_get_icm(requests_mock)
    mock_content_sets_config(workflow.source.path, empty=(not content_sets))
    runner = mock_env(
        workflow, df_content, base_layers, pnc_artifacts=False, remote_sources=REMOTE_SOURCES
    )

    expected_output = deepcopy(ICM_DICT)
    expected_output['image_contents'] = expected_output['image_contents'][:-1]
    expected_output['metadata']['image_layer_index'] = base_layers

    runner.run()

    workflow.build_dir.for_each_platform(
        check_icm(manifest_file, expected_output, has_content_sets=content_sets)
    )
