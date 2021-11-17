"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import copy
import os
import json

from atomic_reactor.constants import (PLUGIN_FETCH_WORKER_METADATA_KEY,
                                      PLUGIN_COMPARE_COMPONENTS_KEY)
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.post_compare_components import (
    filter_components_by_name
)
from atomic_reactor.util import DockerfileImages

from tests.constants import FILES

import pytest


def mock_workflow(workflow):
    setattr(workflow, 'postbuild_result', {})
    workflow.dockerfile_images = DockerfileImages(['fedora:25'])


def mock_metadatas():
    json_x_path = os.path.join(FILES, "example-koji-metadata-x86_64.json")
    json_p_path = os.path.join(FILES, "example-koji-metadata-ppc64le.json")

    with open(json_x_path) as json_data:
        metadatas_x = json.load(json_data)

    with open(json_p_path) as json_data:
        metadatas_p = json.load(json_data)

    # need to keep data separate otherwise deepcopy and edit 'arch'
    worker_metadatas = {
        'x86_64': metadatas_x,
        'ppc64le': metadatas_p,
    }

    return worker_metadatas


def test_filter_components_by_name():
    """Test function filter_components_by_name"""
    worker_metadatas = mock_metadatas()
    component_name = 'openssl'

    component_list = [
        worker_metadata['output'][2]['components']
        for worker_metadata in worker_metadatas.values()
    ]

    filtered = list(filter_components_by_name(component_name, component_list))

    expected_count = len(worker_metadatas)
    assert len(filtered) == expected_count

    expected_platforms = set(worker_metadatas.keys())
    assert set(f['arch'] for f in filtered) == expected_platforms


@pytest.mark.parametrize('base_from_scratch', (True, False))
@pytest.mark.parametrize(('mismatch', 'exception', 'fail'), (
    (False, False, False),
    (True, False, True),
    (False, True, False),
    (True, True, False),
))
def test_compare_components_plugin(workflow, caplog,
                                   base_from_scratch, mismatch, exception, fail):
    mock_workflow(workflow)
    worker_metadatas = mock_metadatas()

    # example data has 2 log items before component item hence output[2]
    component = worker_metadatas['ppc64le']['output'][2]['components'][0]
    if mismatch:
        component['version'] = 'bacon'
    if exception:
        workflow.conf.conf = {'version': 1, 'package_comparison_exceptions': [component['name']]}

    workflow.postbuild_results[PLUGIN_FETCH_WORKER_METADATA_KEY] = worker_metadatas
    if base_from_scratch:
        workflow.dockerfile_images = DockerfileImages(['scratch'])

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
    worker_metadatas = mock_metadatas()

    # example data has 2 log items before component item hence output[2]
    del worker_metadatas['x86_64']['output'][2]['components']
    del worker_metadatas['ppc64le']['output'][2]['components']

    workflow.postbuild_results[PLUGIN_FETCH_WORKER_METADATA_KEY] = worker_metadatas

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
    worker_metadatas = mock_metadatas()

    # example data has 2 log items before component item hence output[2]
    worker_metadatas['x86_64']['output'][2]['components'][0]['type'] = "foo"

    workflow.postbuild_results[PLUGIN_FETCH_WORKER_METADATA_KEY] = worker_metadatas

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
    worker_metadatas = mock_metadatas()

    component_name = "openssl"
    component_ppc64le = worker_metadatas['ppc64le']['output'][2]['components'][4]
    assert component_ppc64le['name'] == component_name, "Error in test data"

    # add extra fake worker for s390x to having 3 different platforms
    # we care about only one component
    worker_metadatas['s390x'] = copy.deepcopy(worker_metadatas['ppc64le'])
    component_s390x = copy.deepcopy(component_ppc64le)
    component_s390x['arch'] = 's390x'
    worker_metadatas['s390x']['output'][2]['components'] = [component_s390x]

    if mismatch:
        component_ppc64le['version'] = 'bacon'
        component_s390x['version'] = 'sandwich'

    workflow.postbuild_results[PLUGIN_FETCH_WORKER_METADATA_KEY] = worker_metadatas

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
