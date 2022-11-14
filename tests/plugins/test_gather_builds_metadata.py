"""
Copyright (c) 2017-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import re
from unittest.mock import patch
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PluginFailedException

from atomic_reactor.utils.koji import get_buildroot
from atomic_reactor.constants import PLUGIN_CHECK_AND_SET_PLATFORMS_KEY
from atomic_reactor.plugins.gather_builds_metadata import GatherBuildsMetadataPlugin
from atomic_reactor.utils.remote_host import RemoteHost
from atomic_reactor.utils.rpm import parse_rpm_output

import pytest
from flexmock import flexmock
import json

from tests.mock_env import MockEnv


X86_64_HOST = 'x86_64_host'
S390X_HOST = 's390x_host'
REMOTE_HOST_CONFIG = {
    "slots_dir": 'slots_dir',
    "pools": {
        "x86_64": {
            X86_64_HOST: {
                "username": 'username',
                "auth": 'keyfile',
                "enabled": True,
                "slots": 1,
                "socket_path": 'socket',
            },
        },
        "s390x": {
            S390X_HOST: {
                "username": 'username',
                "auth": 'keyfile',
                "enabled": True,
                "slots": 1,
                "socket_path": 'socket',
            },
        },
    },
}
REMOTE_HOST_CONFIG_MISSING_X86_64 = {
    "slots_dir": 'slots_dir',
    "pools": {
        "s390x": {
            S390X_HOST: {
                "username": 'username',
                "auth": 'keyfile',
                "enabled": True,
                "slots": 1,
                "socket_path": 'socket',
            },
        },
    },
}
REMOTE_HOST_CONFIG_MISSING_SPECIFIC_X86_64 = {
    "slots_dir": 'slots_dir',
    "pools": {
        "x86_64": {
            'some_x86_64': {
                "username": 'username',
                "auth": 'keyfile',
                "enabled": True,
                "slots": 1,
                "socket_path": 'socket',
            },
        },
        "s390x": {
            S390X_HOST: {
                "username": 'username',
                "auth": 'keyfile',
                "enabled": True,
                "slots": 1,
                "socket_path": 'socket',
            },
        },
    },
}


def mock_reactor_config(workflow, remote_hosts=None):
    config = {
        'version': 1,
        'openshift': {'url': 'openshift_url'},
        'koji': {'hub_url': 'http://brew.host/', 'root_url': '', 'auth': {}},
        'remote_hosts': remote_hosts if remote_hosts else REMOTE_HOST_CONFIG
    }
    workflow.conf.conf = config


class MockedClientSession:
    """Mock koji.ClientSession"""

    def __init__(self, *args, **kwargs):
        pass

    def krb_login(self, *args, **kwargs):
        return True


def test_fail_if_no_platform_is_set(workflow):
    runner = (MockEnv(workflow)
              .for_plugin(GatherBuildsMetadataPlugin.key)
              .create_runner())
    with pytest.raises(PluginFailedException, match="No enabled platforms"):
        runner.run()


def assert_log_file_metadata(metadata, expected_filename, expected_platform, buildroot_id):
    assert metadata["buildroot_id"] == buildroot_id
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
    workflow.data.plugins_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = ["x86_64", "s390x"]
    workflow.data.tag_conf.add_unique_image("ns/img:1.0-1")

    build_log_file = workflow.context_dir.get_platform_build_log("s390x")
    if has_s390x_build_logs:
        build_log_file.write_text("line 1\nline 2\nline 3\n", "utf-8")

    package_list = 'python-docker-py;1.3.1;1.fc24;noarch;(none);191456;' \
                   '7c1f60d8cde73e97a45e0c489f4a3b26;1438058212;(none);(none);(none);(none)\n' \
                   'fedora-repos-rawhide;24;0.1;noarch;(none);2149;' \
                   'd41df1e059544d906363605d47477e60;1436940126;(none);(none);(none);(none)\n' \
                   'gpg-pubkey-doc;1.0;1;noarch;(none);1000;00000000000000000000000000000000;' \
                   '1436940126;(none);(none);(none);(none)'
    all_rpms = [line for line in package_list.splitlines() if line]
    all_components = parse_rpm_output(all_rpms)

    flexmock(RemoteHost).should_receive('rpms_installed').and_return(package_list)

    task_results = {'binary-container-build-x86-64': {'task_result': json.dumps(X86_64_HOST)},
                    'binary-container-build-s390x': {'task_result': json.dumps(S390X_HOST)}}
    flexmock(workflow.osbs).should_receive('get_task_results').and_return(task_results)

    plugin = GatherBuildsMetadataPlugin(workflow)

    with patch("atomic_reactor.plugins.gather_builds_metadata.get_output",
               return_value=([], None)):
        output = plugin.run()

    buildroot_x86_64 = get_buildroot("x86_64")
    buildroot_s390x = get_buildroot("s390x")
    buildroot_x86_64['components'] = all_components
    buildroot_x86_64['id'] = X86_64_HOST
    buildroot_s390x['components'] = all_components
    buildroot_s390x['id'] = S390X_HOST

    expected = {
        "x86_64": {
            "metadata_version": 0,
            "buildroots": [buildroot_x86_64],
            "output": [],
        },
        "s390x": {
            "metadata_version": 0,
            "buildroots": [buildroot_s390x],
            "output": [],
        },
    }

    if has_s390x_build_logs:
        log_file_metadata = output["s390x"]["output"].pop()
        assert_log_file_metadata(
            log_file_metadata,
            expected_filename=build_log_file.name,
            expected_platform="s390x",
            buildroot_id=S390X_HOST
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


@pytest.mark.parametrize(("task_results", "remote_hosts", "error_msg"), (
    ({'binary-container-build-x86-64': {'task_result': json.dumps(X86_64_HOST)}},
     REMOTE_HOST_CONFIG,
     f"unable to obtain installed rpms on: {X86_64_HOST}"),
    ({'binary-container-build-x86-64': {'some_result': json.dumps('some')}},
     REMOTE_HOST_CONFIG,
     "task_results is missing from: binary-container-build-x86-64"),
    ({'some-container-build-x86-64': {'some_result': json.dumps('some')}},
     REMOTE_HOST_CONFIG,
     "unable to find build host for platform: x86_64"),
    ({'binary-container-build-x86-64': {'task_result': json.dumps(X86_64_HOST)}},
     REMOTE_HOST_CONFIG_MISSING_X86_64,
     "unable to find remote hosts for platform: x86_64"),
    ({'binary-container-build-x86-64': {'task_result': json.dumps(X86_64_HOST)}},
     REMOTE_HOST_CONFIG_MISSING_SPECIFIC_X86_64,
     f"unable to get remote host instance: {X86_64_HOST}"),
))
@patch("koji.ClientSession", new=MockedClientSession)
def test_get_build_metadata_fails(task_results, remote_hosts, error_msg,
                                  workflow: DockerBuildWorkflow):
    mock_reactor_config(workflow, remote_hosts=remote_hosts)
    workflow.data.plugins_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = ["x86_64"]
    workflow.data.tag_conf.add_unique_image("ns/img:1.0-1")

    flexmock(RemoteHost).should_receive('rpms_installed').and_return(None)
    flexmock(workflow.osbs).should_receive('get_task_results').and_return(task_results)

    plugin = GatherBuildsMetadataPlugin(workflow)

    with patch("atomic_reactor.plugins.gather_builds_metadata.get_output",
               return_value=([], None)):
        with pytest.raises(RuntimeError, match=error_msg):
            plugin.run()
