"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import io
import json
import re
import shutil
import subprocess
from copy import deepcopy
from json import JSONDecodeError
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict, List, Optional

from osbs.utils import ImageName

import pytest
from flexmock import flexmock

from atomic_reactor import config
from atomic_reactor import dirs
from atomic_reactor import inner
from atomic_reactor import util
from atomic_reactor.config import ReactorConfigKeys
from atomic_reactor.constants import PLUGIN_CHECK_AND_SET_PLATFORMS_KEY
from atomic_reactor.tasks.binary_container_build import (
    BinaryBuildTask,
    BinaryBuildTaskParams,
    # exceptions
    BuildTaskError,
    BuildProcessError,
    ExceedsImageSizeError,
    InspectError,
    PushError,
    # helpers
    PodmanRemote,
    get_authfile_path,
    which_podman,
)
from atomic_reactor.utils import remote_host
from atomic_reactor.utils import retries

CONTEXT_DIR = "/workspace/ws-context-dir"
CONFIG_PATH = "/etc/atomic-reactor/config.yaml"

PIPELINE_RUN_NAME = 'test-pipeline-run'

NOARCH_UNIQUE_IMAGE = ImageName.parse("registry.example.org/osbs/spam:v1.0")
X86_UNIQUE_IMAGE = ImageName.parse("registry.example.org/osbs/spam:v1.0-x86_64")

AUTHFILE_PATH = "/workspace/ws-registries-secret/"
NAMESPACE = "test-namespace"
PIPELINE_RUN_NAME = "binary-container-0-1-123456"
REGISTRY_CONFIG = {
    "url": "https://registry.example.org/v2",
    "insecure": False,
    "auth": True,
}

X86_64 = 'x86_64'

X86_REMOTE_HOST = remote_host.RemoteHost(
    hostname="osbs-remote-host-x86-64-1.example.com",
    username="osbs-podman-dev",
    ssh_keyfile="/workspace/ws-remote-host-auth/remote-host-auth",
    slots=10,
    socket_path="/run/user/2022/podman/podman.sock",
    slots_dir="/run/user/2022/osbs/slots",
)

X86_LOCKED_RESOURCE = remote_host.LockedResource(X86_REMOTE_HOST, X86_64, slot=1,
                                                 prid=PIPELINE_RUN_NAME)

REMOTE_HOST_CONFIG = {
    "slots_dir": X86_REMOTE_HOST.slots_dir,
    "pools": {
        "x86_64": {
            X86_REMOTE_HOST.hostname: {
                "username": X86_REMOTE_HOST.username,
                "auth": X86_REMOTE_HOST.ssh_keyfile,
                "enabled": True,
                "slots": X86_REMOTE_HOST.slots,
                "socket_path": X86_REMOTE_HOST.socket_path,
            },
        },
    },
}

BUILD_ARGS = {"REMOTE_SOURCES": "unpacked_remote_sources"}

DOCKERFILE_CONTENT = dedent(
    """\
    FROM fedora:35

    RUN echo "Hello there."
    """
)


@pytest.fixture
def base_task_params(build_dir: Path, context_dir: Path) -> Dict[str, Any]:
    return {
        "build_dir": str(build_dir),
        "context_dir": str(context_dir),
        "config_file": CONFIG_PATH,
        "namespace": NAMESPACE,
        "pipeline_run_name": PIPELINE_RUN_NAME,
        "user_params": {},
    }


@pytest.fixture
def x86_task_params(base_task_params) -> BinaryBuildTaskParams:
    return BinaryBuildTaskParams(**base_task_params, platform="x86_64")


@pytest.fixture
def aarch64_task_params(base_task_params) -> BinaryBuildTaskParams:
    return BinaryBuildTaskParams(**base_task_params, platform="aarch64")


@pytest.fixture
def x86_build_dir(build_dir: Path) -> dirs.BuildDir:
    x86_dir = build_dir / "x86_64"
    x86_dir.mkdir(exist_ok=True)
    return dirs.BuildDir(x86_dir, "x86_64")


def mock_workflow_data(*, enabled_platforms: List[str]) -> inner.ImageBuildWorkflowData:
    """Make workflow_data return mocked workflow data. Also return this data."""
    tag_conf = inner.TagConf()
    tag_conf.add_unique_image(NOARCH_UNIQUE_IMAGE)

    mocked_data = inner.ImageBuildWorkflowData(
        tag_conf=tag_conf,
        plugins_results={PLUGIN_CHECK_AND_SET_PLATFORMS_KEY: enabled_platforms},
        buildargs=BUILD_ARGS,
    )

    (
        flexmock(BinaryBuildTask)
        .should_receive("workflow_data")
        .and_return(mocked_data)
    )
    return mocked_data


def mock_config(registry_config: Dict[str, Any],
                remote_hosts_config: Dict[str, Any],
                image_size_limit: int = 0) -> config.Configuration:
    """Make load_config() return mocked config.

    The registry property of the mocked config will return the specified registry_config.
    The remote_hosts property will return the remote hosts config.
    """
    raw_config = {'version': 1,
                  ReactorConfigKeys.REGISTRY_KEY: registry_config,
                  ReactorConfigKeys.REMOTE_HOSTS_KEY: remote_hosts_config,
                  ReactorConfigKeys.IMAGE_SIZE_LIMIT_KEY: {'binary_image': image_size_limit},
                  ReactorConfigKeys.REGISTRIES_CFG_PATH_KEY: AUTHFILE_PATH}
    cfg = config.Configuration(raw_config=raw_config)
    flexmock(BinaryBuildTask).should_receive("load_config").and_return(cfg)
    return cfg


class MockedPopen:
    def __init__(self, rc: int, output_lines: List[str]):
        self._rc = rc
        self.stdout = io.StringIO("".join(output_lines))

    def poll(self):
        if self.stdout.tell() >= len(self.stdout.getvalue()):
            # no more output, the process has ended
            return self._rc
        return None


def mock_popen(
    rc: int, output_lines: List[str], expect_cmd: Optional[List[str]] = None
) -> None:
    """Make subprocess.Popen returned a mocked Popen.

    The output_lines should end with '\n', otherwise they wil be combined to a single line.
    Optionally, pass in the expected command to be checked.
    """
    def popen(cmd, *args, **kwargs):
        if expect_cmd:
            assert cmd == expect_cmd
        return MockedPopen(rc, output_lines)

    flexmock(subprocess).should_receive("Popen").replace_with(popen)


class TestBinaryBuildTask:
    """Tests from the BinaryBuildTask class."""

    @pytest.fixture
    def mock_locked_resource(self) -> remote_host.LockedResource:
        (
            flexmock(remote_host.RemoteHostsPool)
            .should_receive("lock_resource")
            .and_return(X86_LOCKED_RESOURCE)
        )
        return X86_LOCKED_RESOURCE

    @pytest.fixture
    def mock_dockercfg_path(self, tmp_path) -> str:
        dockercfg_path = tmp_path / ".dockerconfigjson"
        dockercfg_path.write_text("{}")
        mock_dockercfg = util.Dockercfg(str(dockercfg_path.parent))
        (
            flexmock(util)
            .should_receive("Dockercfg")
            .with_args(AUTHFILE_PATH)
            .and_return(mock_dockercfg)
        )
        return str(dockercfg_path)

    @pytest.fixture
    def mock_podman_remote(self, mock_locked_resource, mock_dockercfg_path) -> PodmanRemote:
        podman_remote = PodmanRemote(connection_name=mock_locked_resource.host.hostname)
        (
            flexmock(PodmanRemote)
            .should_receive("setup_for")
            .with_args(mock_locked_resource, registries_authfile=mock_dockercfg_path)
            .and_return(podman_remote)
        )
        return podman_remote

    def test_platform_is_not_enabled(self, aarch64_task_params, caplog):
        mock_workflow_data(enabled_platforms=["x86_64"])
        flexmock(PodmanRemote).should_receive("build_container").never()

        task = BinaryBuildTask(aarch64_task_params)
        task.execute()

        assert "Platform aarch64 is not enabled for this build" in caplog.text

    @pytest.mark.parametrize('fail_image_size_check', (True, False))
    @pytest.mark.parametrize('is_flatpak', (True, False))
    def test_run_build(
        self, x86_task_params, x86_build_dir, mock_podman_remote, mock_locked_resource, caplog,
            fail_image_size_check, is_flatpak
    ):
        mock_workflow_data(enabled_platforms=["x86_64"])
        if fail_image_size_check:
            mock_config(REGISTRY_CONFIG, REMOTE_HOST_CONFIG, image_size_limit=1233)
        else:
            mock_config(REGISTRY_CONFIG, REMOTE_HOST_CONFIG, image_size_limit=1234)
        x86_build_dir.dockerfile_path.write_text(DOCKERFILE_CONTENT)

        def mock_build_container(*, build_dir, build_args, dest_tag, flatpak):
            assert build_dir.path == x86_build_dir.path
            assert build_dir.platform == "x86_64"
            assert build_args == BUILD_ARGS
            assert dest_tag == X86_UNIQUE_IMAGE
            assert flatpak == is_flatpak

            yield from ["output line 1\n", "output line 2\n"]

        (
            flexmock(mock_podman_remote)
            .should_receive("build_container")
            .once()
            .replace_with(mock_build_container)
        )
        (
            flexmock(mock_podman_remote)
            .should_receive("push_container")
            .with_args(X86_UNIQUE_IMAGE, insecure=REGISTRY_CONFIG["insecure"])
            .times(0 if fail_image_size_check else 1)
        )
        (
            flexmock(mock_podman_remote)
            .should_receive("get_image_size")
            .with_args(X86_UNIQUE_IMAGE)
            .and_return(1234)
            .once()
        )

        flexmock(mock_locked_resource).should_receive("unlock").once()

        x86_task_params.user_params['flatpak'] = is_flatpak

        task = BinaryBuildTask(x86_task_params)
        if fail_image_size_check:
            err_msg = 'The size 1234 of image registry.example.org/osbs/spam:v1.0-x86_64 exceeds ' \
                      'the limitation 1233 configured in reactor config.'
            with pytest.raises(ExceedsImageSizeError, match=err_msg):
                task.execute()
        else:
            task.execute()

        assert (
            f"Building for the x86_64 platform from {x86_build_dir.dockerfile_path}" in caplog.text
        )
        assert "output line 1" in caplog.text
        assert "output line 2" in caplog.text
        assert DOCKERFILE_CONTENT in caplog.text

        build_log_file = Path(x86_task_params.context_dir, 'x86_64-build.log')
        assert build_log_file.exists()
        build_logs = build_log_file.read_text().splitlines()
        assert ["output line 1", "output line 2"] == build_logs

    def test_run_exit_steps_on_failure(
        self, x86_task_params, x86_build_dir, mock_podman_remote, mock_locked_resource, caplog
    ):
        mock_workflow_data(enabled_platforms=["x86_64"])
        mock_config(REGISTRY_CONFIG, REMOTE_HOST_CONFIG)
        x86_build_dir.dockerfile_path.write_text(DOCKERFILE_CONTENT)

        (
            flexmock(mock_podman_remote)
            .should_receive("build_container")
            .and_raise(BuildProcessError("something went wrong"))
        )

        # test that the LockedResource is unlocked on failure
        flexmock(mock_locked_resource).should_receive("unlock").once()

        task = BinaryBuildTask(x86_task_params)
        with pytest.raises(BuildProcessError):
            task.execute()

        # test that the Dockerfile is printed on failure
        assert DOCKERFILE_CONTENT in caplog.text

    def test_acquire_remote_resource_fails(self, x86_task_params):
        pool = remote_host.RemoteHostsPool([X86_REMOTE_HOST], X86_64)
        # also test that the method passes params to the remote_host module correctly
        (
            flexmock(remote_host.RemoteHostsPool)
            .should_receive("from_config")
            .with_args(REMOTE_HOST_CONFIG, "x86_64")
            .once()
            .and_return(pool)
        )
        (
            flexmock(pool)
            .should_receive("lock_resource")
            .with_args(PIPELINE_RUN_NAME)
            .once()
            .and_return(None)
        )

        task = BinaryBuildTask(x86_task_params)

        err_msg = "Failed to acquire a build slot on any remote host!"

        with pytest.raises(BuildTaskError, match=err_msg):
            task.acquire_remote_resource(REMOTE_HOST_CONFIG)


@pytest.mark.parametrize("has_authfile", [True, False])
def test_get_authfile_path(has_authfile, tmp_path):
    dockercfg_path = tmp_path / ".dockerconfigjson"
    dockercfg_path.write_text("{}")

    registry_config = deepcopy(REGISTRY_CONFIG)
    task_config = mock_config(registry_config, REMOTE_HOST_CONFIG)

    if has_authfile:
        task_config.conf["registries_cfg_path"] = str(dockercfg_path.parent)
        assert get_authfile_path(task_config.registry) == str(dockercfg_path)
    else:
        del task_config.conf["registries_cfg_path"]
        assert get_authfile_path(task_config.registry) is None


@pytest.mark.parametrize(
    "podman_path, podman_remote_path, expect_path",
    [
        ("/usr/bin/podman", None, "/usr/bin/podman"),
        (None, "/usr/bin/podman-remote", "/usr/bin/podman-remote"),
        ("/usr/bin/podman", "/usr/bin/podman-remote", "/usr/bin/podman"),
        (None, None, None),
    ],
)
def test_which_podman(podman_path, podman_remote_path, expect_path):
    def mock_which(cmd):
        if cmd == "podman":
            return podman_path
        elif cmd == "podman-remote":
            return podman_remote_path
        else:
            assert False, cmd

    flexmock(shutil).should_receive("which").replace_with(mock_which)

    # make sure which_podman() doesn't return results from the prev. run
    which_podman.cache_clear()

    if expect_path is None:
        err_msg = r"Could not find either podman or podman-remote in \$PATH!"

        with pytest.raises(BuildTaskError, match=err_msg):
            which_podman()
    else:
        assert which_podman() == expect_path


class TestPodmanRemote:
    """Tests for the PodmanRemote class."""

    @pytest.fixture(autouse=True)
    def mock_which_podman(self):
        which_podman.cache_clear()
        flexmock(shutil).should_receive("which").with_args("podman").and_return("/usr/bin/podman")

    def test_setup_for(self):
        resource = X86_LOCKED_RESOURCE
        expect_cmd = [
            "/usr/bin/podman",
            "system",
            "connection",
            "add",
            "--identity=/workspace/ws-remote-host-auth/remote-host-auth",
            "--socket-path=/run/user/2022/podman/podman.sock",
            f'{PIPELINE_RUN_NAME}-{X86_64}',
            "ssh://osbs-podman-dev@osbs-remote-host-x86-64-1.example.com",
        ]
        (
            flexmock(subprocess)
            .should_receive("check_output")
            .with_args(expect_cmd, stderr=subprocess.STDOUT)
            .once()
        )

        podman_remote = PodmanRemote.setup_for(resource)
        assert podman_remote._connection_name == f'{PIPELINE_RUN_NAME}-{X86_64}'

    def test_setup_for_fails(self):
        (
            flexmock(subprocess)
            .should_receive("check_output")
            .and_raise(
                subprocess.CalledProcessError(1, ["podman", "..."], output=b'something went wrong')
            )
        )

        err_msg = "Failed to set up podman-remote connection: something went wrong"

        with pytest.raises(BuildTaskError, match=err_msg):
            PodmanRemote.setup_for(X86_LOCKED_RESOURCE)

    @pytest.mark.parametrize("authfile", [None, AUTHFILE_PATH])
    @pytest.mark.parametrize('is_flatpak', (True, False))
    def test_build_container(self, authfile, is_flatpak, x86_build_dir):
        options = [
            f"--tag={X86_UNIQUE_IMAGE}",
            "--no-cache",
            "--pull-always",
        ]
        if is_flatpak:
            options.append("--squash-all")
            for device in ['null', 'random', 'urandom', 'zero']:
                options.append(f"--device=/dev/{device}:/var/tmp/flatpak-build/dev/{device}")
        else:
            options.append("--squash")
        options.append("--build-arg=REMOTE_SOURCES=unpacked_remote_sources")
        if authfile:
            options.append(f"--authfile={authfile}")

        expect_cmd = [
            "/usr/bin/podman",
            "--remote",
            "--connection=connection-name",
            "build",
            *options,
            str(x86_build_dir.path),
        ]

        mock_popen(0, ["starting the build\n", "finished successfully\n"], expect_cmd=expect_cmd)

        podman_remote = PodmanRemote("connection-name", registries_authfile=authfile)
        output_lines = podman_remote.build_container(
            build_dir=x86_build_dir,
            build_args=BUILD_ARGS,
            dest_tag=X86_UNIQUE_IMAGE,
            flatpak=is_flatpak
        )

        assert list(output_lines) == ["starting the build\n", "finished successfully\n"]

    @pytest.mark.parametrize(
        "output_lines, expect_err_line",
        [
            (["starting the build\n", "failed :(\n"], "failed :("),
            (["failed and printed an empty line\n", "\n"], "failed and printed an empty line"),
            ([], "<no output!>"),
            (["\n"], "<no output!>"),
        ]
    )
    def test_build_container_fails(
        self, output_lines, expect_err_line, x86_build_dir
    ):
        mock_popen(1, output_lines)

        podman_remote = PodmanRemote("connection-name")
        returned_lines = podman_remote.build_container(
            build_dir=x86_build_dir,
            build_args=BUILD_ARGS,
            dest_tag=X86_UNIQUE_IMAGE,
            flatpak=False,
        )

        for expect_line in output_lines:
            assert next(returned_lines) == expect_line

        err_msg = rf"Build failed \(rc=1\). {re.escape(expect_err_line)}"

        with pytest.raises(BuildProcessError, match=err_msg):
            next(returned_lines)

    @pytest.mark.parametrize("authfile", [None, AUTHFILE_PATH])
    @pytest.mark.parametrize("insecure", [True, False])
    def test_push_container(self, authfile, insecure):
        expect_cmd = [
            "/usr/bin/podman",
            "--remote",
            "--connection=connection-name",
            "push",
            "--format=v2s2",
            str(X86_UNIQUE_IMAGE),
        ]
        if authfile:
            expect_cmd.insert(-1, f"--authfile={authfile}")
        if insecure:
            expect_cmd.insert(-1, "--tls-verify=false")

        flexmock(retries).should_receive("run_cmd").with_args(expect_cmd).once()

        podman_remote = PodmanRemote("connection-name", registries_authfile=authfile)
        podman_remote.push_container(X86_UNIQUE_IMAGE, insecure=insecure)

    def test_push_container_fails(self):
        (
            flexmock(retries)
            .should_receive("run_cmd")
            .and_raise(subprocess.CalledProcessError(1, 'some command'))
        )

        podman_remote = PodmanRemote("connection-name")

        err_msg = r"Push failed \(rc=1\). Check the logs for more details."

        with pytest.raises(PushError, match=err_msg):
            podman_remote.push_container(X86_UNIQUE_IMAGE)

    @pytest.mark.parametrize(('error', 'error_msg'), [
        (None, None),
        (IndexError, "inspect didn't return any results"),
        (JSONDecodeError, 'inspect returned invalid JSON'),
        (InspectError, "Couldn't check image size"),
    ])
    def test_get_image_size(self, x86_task_params, error, error_msg):
        expect_cmd = [
            "/usr/bin/podman",
            "--remote",
            "--connection=connection-name",
            "image",
            "inspect",
            str(X86_UNIQUE_IMAGE),
        ]

        if error == IndexError:
            inspect_json = json.dumps([])
        elif error == JSONDecodeError:
            inspect_json = 'invalid json'
        else:
            inspect_json = json.dumps(
                [
                    {
                        "Id": "750037c05cfe1857e16500167c7c217658e15eb9bc6283020cfb3524c93d1240",
                        "Digest": "sha256:1fcb4b8b5d3fcdba78119734db03328fb3b8463bbcc83e1dda3e4"
                                  "ffb0ffe3b34",
                        "Size": 158641422,
                    }
                ]
            )
        if error != InspectError:
            (
                flexmock(retries)
                .should_receive("run_cmd")
                .with_args(expect_cmd).once()
                .and_return(inspect_json)
            )
        else:
            (
                flexmock(retries)
                .should_receive("run_cmd")
                .and_raise(subprocess.CalledProcessError(1, "some command"))
            )

        podman_remote = PodmanRemote("connection-name")

        if error:
            # all errors are re-raised as InspectError
            with pytest.raises(InspectError, match=error_msg):
                podman_remote.get_image_size(X86_UNIQUE_IMAGE)
        else:
            podman_remote.get_image_size(X86_UNIQUE_IMAGE)
