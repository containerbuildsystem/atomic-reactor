"""
Copyright (c) 2017-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import copy
import os
import json

from atomic_reactor.constants import PLUGIN_COMPARE_COMPONENTS_KEY
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.post_compare_components import (
    filter_components_by_name
)
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin
from atomic_reactor.util import DockerfileImages

from tests.constants import FILES

import pytest


def mock_workflow(workflow):
    setattr(workflow, 'postbuild_result', {})
    workflow.data.dockerfile_images = DockerfileImages(['fedora:25'])


def mock_components():
    json_x_path = os.path.join(FILES, "example-koji-metadata-x86_64.json")
    json_p_path = os.path.join(FILES, "example-koji-metadata-ppc64le.json")

    with open(json_x_path) as json_data:
        metadatas_x = json.load(json_data)

    with open(json_p_path) as json_data:
        metadatas_p = json.load(json_data)

    # need to keep data separate otherwise deepcopy and edit 'arch'
    components = {
        'x86_64': metadatas_x['output'][2]['components'],
        'ppc64le': metadatas_p['output'][2]['components'],
    }

    return components


def test_filter_components_by_name():
    """Test function filter_components_by_name"""
    components_per_arch = mock_components()
    component_name = 'openssl'

    component_list = [components for components in components_per_arch.values()]

    filtered = list(filter_components_by_name(component_name, component_list))

    expected_count = len(components_per_arch)
    assert len(filtered) == expected_count

    expected_platforms = set(components_per_arch.keys())
    assert set(f['arch'] for f in filtered) == expected_platforms


@pytest.mark.parametrize('base_from_scratch', (True, False))
@pytest.mark.parametrize(('mismatch', 'exception', 'fail'), (
    (False, False, False),
    (True, False, True),
    (False, True, False),
    (True, True, False),
))
def test_compare_components_plugin(workflow, caplog, base_from_scratch, mismatch, exception, fail):
    mock_workflow(workflow)
    components_per_arch = mock_components()

    # example data has 2 log items before component item hence output[2]
    component = components_per_arch['ppc64le'][0]
    if mismatch:
        component['version'] = 'bacon'
    if exception:
        workflow.conf.conf = {'version': 1, 'package_comparison_exceptions': [component['name']]}

    workflow.data.postbuild_results[PostBuildRPMqaPlugin.key] = components_per_arch
    if base_from_scratch:
        workflow.data.dockerfile_images = DockerfileImages(['scratch'])

    runner = PostBuildPluginsRunner(
        workflow,
        [{
            'name': PLUGIN_COMPARE_COMPONENTS_KEY,
            "args": {}
        }]
    )

    if fail and not base_from_scratch:
        with pytest.raises(PluginFailedException):
            runner.run()
    else:
        runner.run()
        if base_from_scratch:
            log_msg = "Skipping comparing components: unsupported for FROM-scratch images"
            assert log_msg in caplog.text


def test_no_components(workflow):
    mock_workflow(workflow)
    components_per_arch = mock_components()

    # example data has 2 log items before component item hence output[2]
    components_per_arch['x86_64'] = None
    components_per_arch['ppc64le'] = None

    workflow.data.postbuild_results[PostBuildRPMqaPlugin.key] = components_per_arch

    runner = PostBuildPluginsRunner(
        workflow,
        [{
            'name': PLUGIN_COMPARE_COMPONENTS_KEY,
            "args": {}
        }]
    )

    with pytest.raises(PluginFailedException):
        runner.run()


def test_bad_component_type(workflow):
    mock_workflow(workflow)
    components_per_arch = mock_components()

    # example data has 2 log items before component item hence output[2]
    components_per_arch['x86_64'][0]['type'] = "foo"

    workflow.data.postbuild_results[PostBuildRPMqaPlugin.key] = components_per_arch

    runner = PostBuildPluginsRunner(
        workflow,
        [{
            'name': PLUGIN_COMPARE_COMPONENTS_KEY,
            "args": {}
        }]
    )

    with pytest.raises(PluginFailedException):
        runner.run()


@pytest.mark.parametrize('mismatch', (True, False))
def test_mismatch_reporting(workflow, caplog, mismatch):
    """Test if expected log entries are reported when components mismatch"""
    mock_workflow(workflow)
    components_per_arch = mock_components()

    component_name = "openssl"
    component_ppc64le = components_per_arch['ppc64le'][4]
    assert component_ppc64le['name'] == component_name, "Error in test data"

    # add extra fake worker for s390x to having 3 different platforms
    # we care about only one component
    components_per_arch['s390x'] = copy.deepcopy(components_per_arch['ppc64le'])
    component_s390x = copy.deepcopy(component_ppc64le)
    component_s390x['arch'] = 's390x'
    components_per_arch['s390x'] = [component_s390x]

    if mismatch:
        component_ppc64le['version'] = 'bacon'
        component_s390x['version'] = 'sandwich'

    workflow.data.postbuild_results[PostBuildRPMqaPlugin.key] = components_per_arch

    runner = PostBuildPluginsRunner(
        workflow,
        [{
            'name': PLUGIN_COMPARE_COMPONENTS_KEY,
            "args": {}
        }]
    )

    log_entries = (
        'Comparison mismatch for component openssl:',
        'ppc64le: openssl-bacon-8.el7 (199e2f91fd431d51)',
        'x86_64: openssl-1.0.2k-8.el7 (199e2f91fd431d51)',
        's390x: openssl-sandwich-8.el7 (199e2f91fd431d51)',
    )

    if mismatch:
        # mismatch detected, failure and log entries are expected
        with pytest.raises(PluginFailedException):
            try:
                runner.run()
            except PluginFailedException as e:
                assert 'Failed component comparison for components: openssl' in str(e)
                raise

        for entry in log_entries:
            # component mismatch must be reported only once
            assert caplog.text.count(entry) == 1
    else:
        # no mismatch, no failure, no log entries
        runner.run()
        for entry in log_entries:
            assert entry not in caplog.text


def test_skip_plugin(workflow, caplog):
    mock_workflow(workflow)
    workflow.user_params['scratch'] = True

    runner = PostBuildPluginsRunner(
        workflow,
        [{
            'name': PLUGIN_COMPARE_COMPONENTS_KEY,
            "args": {}
        }]
    )

    runner.run()

    assert 'scratch build, skipping plugin' in caplog.text
