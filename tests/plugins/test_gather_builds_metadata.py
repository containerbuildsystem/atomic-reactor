"""
Copyright (c) 2017-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import re
from unittest.mock import patch
from atomic_reactor.inner import DockerBuildWorkflow

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


def assert_log_file_metadata(metadata, expected_filename, expected_platform):
    assert metadata["buildroot_id"] == 1
    assert metadata["type"] == "log"
    assert metadata["arch"] == expected_platform
    assert metadata["filename"] == expected_filename
    assert metadata["filesize"] > 0
    assert re.match(r"^[0-9a-z]+$", metadata["checksum"])
    assert metadata["checksum_type"] == "md5"


@pytest.mark.parametrize("has_s390x_build_logs", [True, False])
@patch("koji.ClientSession", new=MockedClientSession)
def test_gather_builds_metadata(has_s390x_build_logs, workflow: DockerBuildWorkflow, caplog):
    mock_reactor_config(workflow)
    workflow.data.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = ["x86_64", "s390x"]
    workflow.data.tag_conf.add_unique_image("ns/img:1.0-1")

    build_log_file = workflow.context_dir.get_platform_build_log("s390x")
    if has_s390x_build_logs:
        build_log_file.write_text("line 1\nline 2\nline 3\n", "utf-8")

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

    if has_s390x_build_logs:
        log_file_metadata = output["s390x"]["output"].pop()
        assert_log_file_metadata(
            log_file_metadata,
            expected_filename=build_log_file.name,
            expected_platform="s390x",
        )

        upload_file_info = {
            "local_filename": str(build_log_file),
            "dest_filename": build_log_file.name,
        }
        assert [upload_file_info] == workflow.data.koji_upload_files
    else:
        assert re.search(r"not found: .+s390x-build.log", caplog.text)

    # There is always no build log for x86_64 in this test.
    assert re.search(r"not found: .+x86_64-build.log", caplog.text)
    assert expected == output
