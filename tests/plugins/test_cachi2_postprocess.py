"""
Copyright (c) 2024 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import json
import io
import tarfile
from collections import namedtuple
from pathlib import Path
from textwrap import dedent
from typing import Callable, Dict

import pytest
import yaml

from atomic_reactor.dirs import BuildDir
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.constants import (
    CACHI2_BUILD_DIR,
    CACHI2_BUILD_APP_DIR,
    CACHI2_SINGLE_REMOTE_SOURCE_NAME,
    CACHITO_ENV_ARG_ALIAS,
    CACHITO_ENV_FILENAME,
    REMOTE_SOURCE_DIR,
    REMOTE_SOURCE_TARBALL_FILENAME,
    REMOTE_SOURCE_JSON_FILENAME,
    REMOTE_SOURCE_JSON_ENV_FILENAME,
    PLUGIN_CACHI2_INIT,
)
from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.cachi2_postprocess import (
    Cachi2PostprocessPlugin,
    Cachi2RemoteSource,
)
from atomic_reactor.source import SourceConfig
from atomic_reactor.utils.cachi2 import generate_request_json

from tests.mock_env import MockEnv
from tests.stubs import StubSource


FIRST_REMOTE_SOURCE_NAME = "first"
SECOND_REMOTE_SOURCE_NAME = "second"
REMOTE_SOURCE_REPO = 'https://git.example.com/team/repo.git'
REMOTE_SOURCE_REF = 'b55c00f45ec3dfee0c766cea3d395d6e21cc2e5a'
SECOND_REMOTE_SOURCE_REPO = 'https://git.example.com/other-team/other-repo.git'
SECOND_REMOTE_SOURCE_REF = 'd55c00f45ec3dfee0c766cea3d395d6e21cc2e5c'


RemoteSourceInitResult = namedtuple('RemoteSourceInitResult', ['result', 'env_vars', 'sbom'])


def mock_cachi2_init_and_run_plugin(
        workflow, *args: RemoteSourceInitResult):

    plugin_result = []

    for arg in args:
        plugin_result.append(arg.result)

        source_root_path = Path(arg.result["source_path"])
        source_root_path.mkdir(parents=True)

        app_dir = source_root_path / CACHI2_BUILD_APP_DIR
        app_dir.mkdir()

        name = arg.result["name"] or "single source"
        with open(app_dir / "app.txt", "w") as f:
            f.write(f"test app {name}")
            f.flush()

        deps_dir = source_root_path / "deps"
        deps_dir.mkdir()
        with open(deps_dir / "dep.txt", "w") as f:
            f.write(f"dependency for {name}")
            f.flush()

        with open(source_root_path / "cachi2.env.json", "w") as f:
            json.dump(arg.env_vars, f)

        with open(source_root_path / "bom.json", "w") as f:
            json.dump(arg.sbom, f)

        mock_cachi2_output_tarball(source_root_path / "remote-source.tar.gz")

    workflow.data.plugins_results[PLUGIN_CACHI2_INIT] = plugin_result


def mock_reactor_config(workflow, data=None):
    config = yaml.safe_load(data)
    workflow.conf.conf = config


def mock_repo_config(workflow, data=None):
    if data is None:
        data = dedent("""\
            remote_source:
                repo: {}
                ref: {}
            """.format(REMOTE_SOURCE_REPO, REMOTE_SOURCE_REF))

    workflow._tmpdir.joinpath('container.yaml').write_text(data, "utf-8")

    # The repo config is read when SourceConfig is initialized. Force
    # reloading here to make usage easier.
    workflow.source.config = SourceConfig(str(workflow._tmpdir))


@pytest.fixture
def workflow(workflow: DockerBuildWorkflow, source_dir):
    # Stash the tmpdir in workflow so it can be used later
    workflow._tmpdir = source_dir

    class MockSource(StubSource):

        def __init__(self, workdir):
            super(MockSource, self).__init__()
            self.workdir = workdir
            self.path = workdir

    workflow.source = MockSource(str(source_dir))

    mock_repo_config(workflow)

    workflow.build_dir.init_build_dirs(["x86_64", "ppc64le"], workflow.source)

    return workflow


def expected_build_dir(workflow) -> str:
    """The primary build_dir that the plugin is expected to work with."""
    return str(workflow.build_dir.any_platform.path)


def mock_cachi2_output_tarball(create_at_path) -> str:
    """Create a mocked tarball for a remote source at the specified path."""
    create_at_path = Path(create_at_path)
    file_content = f"Content of {create_at_path.name}".encode("utf-8")

    readme = tarfile.TarInfo("app/app.txt")
    readme.size = len(file_content)

    with tarfile.open(create_at_path, 'w:gz') as tar:
        tar.addfile(readme, io.BytesIO(file_content))

    return str(create_at_path)


def check_injected_files(expected_files: Dict[str, str]) -> Callable[[BuildDir], None]:
    """Make a callable that checks expected files in a BuildDir."""

    def check_files(build_dir: BuildDir) -> None:
        """Check the presence and content of files in the unpacked_remote_sources directory."""
        unpacked_remote_sources = build_dir.path / Cachi2PostprocessPlugin.REMOTE_SOURCE

        for path, expected_content in expected_files.items():
            abspath = unpacked_remote_sources / path
            assert abspath.read_text() == expected_content

    return check_files


def test_skip_when_no_results_from_init(workflow):
    """Plugin should skip if there are no results from cachi2_init plugin"""
    assert run_plugin_with_args(workflow) is None


def test_resolve_remote_source_single(workflow):

    remote_source_sbom = {
        "bomFormat": "CycloneDX",
        "components": [
            {
                "name": "bytes",
                "purl": "pkg:golang/bytes?type=package",
                "properties": [
                    {
                        "name": "cachi2:found_by",
                        "value": "cachi2"
                    }
                ],
                "type": "library"
            },
        ],
    }

    remote_source_env_json = [
        {
          "name": "GOCACHE",
          "value": "/remote-source/deps/gomod",
        },
    ]

    single_source = {
        "name": None,
        "source_path": str(
            workflow.build_dir.path / CACHI2_BUILD_DIR / CACHI2_SINGLE_REMOTE_SOURCE_NAME),
        "remote_source": {
            "repo": REMOTE_SOURCE_REPO,
            "ref": REMOTE_SOURCE_REF,
        }
    }

    mock_cachi2_init_and_run_plugin(
        workflow,
        RemoteSourceInitResult(
            single_source, remote_source_env_json, remote_source_sbom
        )
    )
    expected_plugin_results = [
        {
            "name": None,
            "remote_source_json": {
                "json": generate_request_json(
                    single_source["remote_source"], remote_source_sbom,
                    remote_source_env_json),
                "filename": REMOTE_SOURCE_JSON_FILENAME,
            },
            "remote_source_json_env": {
                "json": remote_source_env_json,
                "filename": REMOTE_SOURCE_JSON_ENV_FILENAME,
            },
            "remote_source_tarball": {
                "filename": REMOTE_SOURCE_TARBALL_FILENAME,
                "path": str(Path(single_source["source_path"]) / "remote-source.tar.gz"),
            },
        },
    ]

    run_plugin_with_args(
        workflow,
        expected_plugin_results=expected_plugin_results,
    )

    cachito_env_content = dedent(
        """\
        #!/bin/bash
        export GOCACHE=/remote-source/deps/gomod
        """
    )

    workflow.build_dir.for_each_platform(
        check_injected_files(
            {
                "cachito.env": cachito_env_content,
                "app/app.txt": "test app single source",
                "deps/dep.txt": "dependency for single source",
            },
        )
    )

    assert workflow.data.buildargs == {
        "GOCACHE": "/remote-source/deps/gomod",
        "REMOTE_SOURCE": Cachi2PostprocessPlugin.REMOTE_SOURCE,
        "REMOTE_SOURCE_DIR": REMOTE_SOURCE_DIR,
        CACHITO_ENV_ARG_ALIAS: str(Path(REMOTE_SOURCE_DIR, CACHITO_ENV_FILENAME)),
    }


def test_multiple_remote_sources(workflow):

    container_yaml_config = dedent(
        f"""\
                remote_sources:
                - name: {FIRST_REMOTE_SOURCE_NAME}
                  remote_source:
                    repo: {REMOTE_SOURCE_REPO}
                    ref: {REMOTE_SOURCE_REF}
                - name: {SECOND_REMOTE_SOURCE_NAME}
                  remote_source:
                    repo: {REMOTE_SOURCE_REPO}
                    ref: {REMOTE_SOURCE_REF}
                """
    )

    reactor_config = dedent("""\
                version: 1
                allow_multiple_remote_sources: true
                """)

    first_remote_source_sbom = {
        "bomFormat": "CycloneDX",
        "components": [
            {
                "name": "bytes",
                "purl": "pkg:golang/bytes?type=package",
                "properties": [
                    {
                        "name": "cachi2:found_by",
                        "value": "cachi2"
                    }
                ],
                "type": "library"
            },
        ],
    }

    second_remote_source_sbom = {
        "bomFormat": "CycloneDX",
        "components": [
            {
                "name": "bytes",
                "purl": "pkg:pip/bytes?type=package",
                "properties": [
                    {
                        "name": "cachi2:found_by",
                        "value": "cachi2"
                    }
                ],
                "type": "library"
            },
        ],
    }

    first_remote_source_env_json = [
        {
          "name": "GOCACHE",
          "value": "/remote-source/deps/gomod",
        },
    ]

    second_remote_source_env_json = [
        {
          "name": "PIP_INDEX",
          "value": "/remote-source/deps/somewhere-here",
        },
    ]

    first_source = {
        "name": FIRST_REMOTE_SOURCE_NAME,
        "source_path": str(workflow.build_dir.path / CACHI2_BUILD_DIR / FIRST_REMOTE_SOURCE_NAME),
        "remote_source": {
            "repo": REMOTE_SOURCE_REPO,
            "ref": REMOTE_SOURCE_REF,
            "pkg_managers": ["gomod"],
            "flags": ["gomod-vendor"],
        }
    }

    second_source = {
        "name": SECOND_REMOTE_SOURCE_NAME,
        "source_path": str(workflow.build_dir.path / CACHI2_BUILD_DIR / SECOND_REMOTE_SOURCE_NAME),
        "remote_source": {
            "repo": SECOND_REMOTE_SOURCE_REPO,
            "ref": SECOND_REMOTE_SOURCE_REF,
        }
    }

    mock_repo_config(workflow, data=container_yaml_config)
    mock_reactor_config(workflow, reactor_config)
    mock_cachi2_init_and_run_plugin(
        workflow,
        RemoteSourceInitResult(
            first_source, first_remote_source_env_json, first_remote_source_sbom),
        RemoteSourceInitResult(
            second_source, second_remote_source_env_json, second_remote_source_sbom),
    )
    expected_plugin_results = [
        {
            "name": FIRST_REMOTE_SOURCE_NAME,
            "remote_source_json": {
                "json": generate_request_json(
                    first_source["remote_source"], first_remote_source_sbom,
                    first_remote_source_env_json),
                "filename": "remote-source-first.json",
            },
            "remote_source_json_env": {
                "json": first_remote_source_env_json,
                "filename": "remote-source-first.env.json",
            },
            "remote_source_tarball": {
                "filename": "remote-source-first.tar.gz",
                "path": str(Path(first_source["source_path"]) / "remote-source.tar.gz"),
            },
        },
        {
            "name": SECOND_REMOTE_SOURCE_NAME,
            "remote_source_json": {
                "json": generate_request_json(
                    second_source["remote_source"], second_remote_source_sbom,
                    second_remote_source_env_json),
                "filename": "remote-source-second.json",
            },
            "remote_source_json_env": {
                "json": second_remote_source_env_json,
                "filename": "remote-source-second.env.json",
            },
            "remote_source_tarball": {
                "filename": "remote-source-second.tar.gz",
                "path": str(Path(second_source["source_path"]) / "remote-source.tar.gz"),
            },
        },
    ]

    run_plugin_with_args(workflow, expected_plugin_results=expected_plugin_results)

    first_cachito_env = dedent(
        """\
        #!/bin/bash
        export GOCACHE=/remote-source/deps/gomod
        """
    )
    second_cachito_env = dedent(
        """\
        #!/bin/bash
        export PIP_INDEX=/remote-source/deps/somewhere-here
        """
    )

    workflow.build_dir.for_each_platform(
        check_injected_files(
            {
                f"{FIRST_REMOTE_SOURCE_NAME}/cachito.env": first_cachito_env,
                f"{FIRST_REMOTE_SOURCE_NAME}/app/app.txt": f"test app {FIRST_REMOTE_SOURCE_NAME}",
                f"{FIRST_REMOTE_SOURCE_NAME}/deps/dep.txt": (
                    f"dependency for {FIRST_REMOTE_SOURCE_NAME}"),
                f"{SECOND_REMOTE_SOURCE_NAME}/cachito.env": second_cachito_env,
                f"{SECOND_REMOTE_SOURCE_NAME}/app/app.txt": f"test app {SECOND_REMOTE_SOURCE_NAME}",
                f"{SECOND_REMOTE_SOURCE_NAME}/deps/dep.txt": (
                    f"dependency for {SECOND_REMOTE_SOURCE_NAME}"),
            },
        )
    )

    assert workflow.data.buildargs == {
        "REMOTE_SOURCES": Cachi2PostprocessPlugin.REMOTE_SOURCE,
        "REMOTE_SOURCES_DIR": REMOTE_SOURCE_DIR,
    }


def run_plugin_with_args(workflow, expect_error=None,
                         expect_result=True, expected_plugin_results=None):
    runner = (MockEnv(workflow)
              .for_plugin(Cachi2PostprocessPlugin.key)
              .create_runner())

    if expect_error:
        with pytest.raises(PluginFailedException, match=expect_error):
            runner.run()
        return

    results = runner.run()[Cachi2PostprocessPlugin.key]

    if expect_result:
        assert results == expected_plugin_results

    return results


def test_inject_remote_sources_dest_already_exists(workflow):
    plugin = Cachi2PostprocessPlugin(workflow)

    processed_remote_sources = [
        Cachi2RemoteSource(
            name=None,
            json_data={},
            json_env_data={},
            tarball_path=Path("/does/not/matter"),
            sources_path="/"
        ),
    ]

    builddir_path = Path(expected_build_dir(workflow))
    builddir_path.joinpath(Cachi2PostprocessPlugin.REMOTE_SOURCE).mkdir()

    err_msg = "Conflicting path unpacked_remote_sources already exists"
    with pytest.raises(RuntimeError, match=err_msg):
        plugin.inject_remote_sources(processed_remote_sources)


def test_generate_cachito_env_file_shell_quoting(workflow):
    plugin = Cachi2PostprocessPlugin(workflow)

    dest_dir = Path(expected_build_dir(workflow))
    plugin.generate_cachito_env_file(dest_dir, {"foo": "somefile; rm -rf ~"})

    cachito_env = dest_dir / "cachito.env"
    assert cachito_env.read_text() == dedent(
        """\
        #!/bin/bash
        export foo='somefile; rm -rf ~'
        """
    )
