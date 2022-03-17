"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from unittest.mock import patch

from atomic_reactor.utils.koji import get_buildroot
from atomic_reactor.constants import PLUGIN_CHECK_AND_SET_PLATFORMS_KEY
from atomic_reactor.plugin import PluginFailedException, PostBuildPluginsRunner
from atomic_reactor.plugins.post_gather_builds_metadata import GatherBuildsMetadataPlugin

import pytest


def mock_reactor_config(workflow):
    config = {
        'version': 1,
        'koji': {'hub_url': 'http://brew.host/', 'root_url': '', 'auth': {}},
    }
    workflow.conf.conf = config


class MockedClientSession:
    """Mock koji.ClientSession"""

    def __init__(self, *args, **kwargs):
        pass

    def krb_login(self, *args, **kwargs):
        return True


def test_fail_if_no_platform_is_set(workflow):
    runner = PostBuildPluginsRunner(
        workflow,
        [{
            'name': GatherBuildsMetadataPlugin.key,
            'args': {
                "koji_upload_dir": "path/to/upload",
            }
        }],
    )
    with pytest.raises(PluginFailedException, match="No enabled platforms"):
        runner.run()


@patch("koji.ClientSession", new=MockedClientSession)
def test_gather_builds_metadata(workflow):
    mock_reactor_config(workflow)
    workflow.data.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = ["x86_64", "s390x"]
    workflow.data.tag_conf.add_unique_image("ns/img:1.0-1")

    plugin = GatherBuildsMetadataPlugin(workflow, koji_upload_dir="path/to/upload")

    with patch("atomic_reactor.plugins.post_gather_builds_metadata.get_output",
               return_value=([], None)):
        output = plugin.run()

    expected = {
        "x86_64": {
            "metadata_version": 0,
            "buildroots": [get_buildroot("x86_64")],
            "output": [],
        },
        "s390x": {
            "metadata_version": 0,
            "buildroots": [get_buildroot("s390x")],
            "output": [],
        },
    }
    assert expected == output
