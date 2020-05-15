"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals, absolute_import

import os
import pytest
import json
import yaml
from flexmock import flexmock

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_add_content_sets import AddContentSetsPlugin
from atomic_reactor.constants import INSPECT_ROOTFS, INSPECT_ROOTFS_LAYERS
from atomic_reactor.util import df_parser
from tests.constants import SOURCE
from tests.stubs import StubInsideBuilder, StubSource
from textwrap import dedent


PULP_MAPPING = {'x86_64': ['pulp-spamx86-rpms', 'pulp-baconx86-rpms'],
                'ppc64': ['pulp-spamppc64-rpms', 'pulp-baconppc64-rpms'],
                's390x': ['pulp-spams390x-rpms', 'pulp-bacons390x-rpms']}


def mock_workflow():
    workflow = DockerBuildWorkflow("mock:default_built", source=SOURCE)
    workflow.source = StubSource()
    builder = StubInsideBuilder().for_workflow(workflow)
    builder.set_df_path('/mock-path')
    builder.tasker = flexmock()
    workflow.builder = flexmock(builder)

    return workflow


def run_plugin(workflow, docker_tasker):
    result = PreBuildPluginsRunner(
       docker_tasker, workflow,
       [{
          'name': AddContentSetsPlugin.key,
          'args': {},
       }]
    ).run()

    return result[AddContentSetsPlugin.key]


def mock_content_sets_config(tmpdir, empty=False):
    content_dict = {}
    if not empty:
        for arch, repos in PULP_MAPPING.items():
            content_dict[arch] = repos

    tmpdir.join('content_sets.yml').write(yaml.safe_dump(content_dict))


@pytest.mark.parametrize('manifest_file_exists', [True, False])
@pytest.mark.parametrize('content_sets', [True, False])
@pytest.mark.parametrize('platform', ['x86_64', 'ppc64', 's390x'])
@pytest.mark.parametrize(('df_content, expected_df, base_layers, manifest_file'), [
    (
        dedent("""\
            FROM base_image
            CMD build /spam/eggs
            LABEL com.redhat.component=eggs version=1.0 release=42
        """),
        dedent("""\
            FROM base_image
            CMD build /spam/eggs
            ADD content_manifest.eggs-1.0-42.json /root/buildinfo/content_manifests/eggs-1.0-42.json
            LABEL com.redhat.component=eggs version=1.0 release=42
        """),
        2,
        'content_manifest.eggs-1.0-42.json',
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
            ADD content_manifest.eggs-1.0-42.json /root/buildinfo/content_manifests/eggs-1.0-42.json
            LABEL com.redhat.component=eggs version=1.0 release=42
        """),
        3,
        'content_manifest.eggs-1.0-42.json',
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
            ADD content_manifest.eggs-1.0-42.json /root/buildinfo/content_manifests/eggs-1.0-42.json
            LABEL com.redhat.component=eggs version=1.0 release=42
        """),
        0,
        'content_manifest.eggs-1.0-42.json',
    ),
])
def test_add_content_sets(tmpdir, caplog, docker_tasker, platform, manifest_file_exists,
                          content_sets, df_content, expected_df, base_layers, manifest_file):
    mock_content_sets_config(tmpdir, empty=not content_sets)
    dfp = df_parser(str(tmpdir))
    dfp.content = df_content

    if manifest_file_exists:
        tmpdir.join(manifest_file).write("")

    layer_index = base_layers if base_layers else 1
    expected_output_json = {'metadata': {'image_layer_index': layer_index}, 'content_sets': []}
    if content_sets:
        expected_output_json['content_sets'] = PULP_MAPPING[platform]

    workflow = mock_workflow()
    workflow.user_params['platform'] = platform
    workflow.builder.set_df_path(dfp.dockerfile_path)

    inspection_data = {INSPECT_ROOTFS: {INSPECT_ROOTFS_LAYERS: list(range(base_layers))}}
    workflow.builder.set_inspection_data(inspection_data)

    if manifest_file_exists:
        with pytest.raises(PluginFailedException):
            run_plugin(workflow, docker_tasker)

        log_msg = 'file {} already exists in repo'.format(os.path.join(str(tmpdir), manifest_file))
        assert log_msg in caplog.text
        return

    run_plugin(workflow, docker_tasker)

    assert dfp.content == expected_df

    output_file = os.path.join(str(tmpdir), manifest_file)
    with open(output_file) as f:
        json_data = f.read()
    output_json = json.loads(json_data)

    assert output_json == expected_output_json
