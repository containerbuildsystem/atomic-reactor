"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import pytest

from atomic_reactor import source
from atomic_reactor.tasks import sources


@pytest.fixture
def params(build_dir) -> sources.SourceBuildTaskParams:
    return sources.SourceBuildTaskParams(
        # build_dir has to be an existing directory because DummySource creates a subdirectory
        #   as soon as an instance is created
        build_dir=str(build_dir),
        context_dir="/context",
        config_file="config.yaml",
        user_params={},
    )


class TestSourceBuildTaskParams:
    """Tests for the SourceBuildTaskParams class."""

    def test_source_property(self, params):
        src = params.source

        assert isinstance(src, source.DummySource)
        assert src.workdir == params.build_dir


class TestSourceBuildTask:
    """Tests for the SourceBuildTask class."""

    def test_prepare_workflow(self, params):
        task = sources.SourceBuildTask(params)

        workflow = task.prepare_workflow()
        assert workflow.build_dir.platforms == ["noarch"]
        assert workflow.build_dir.has_sources
