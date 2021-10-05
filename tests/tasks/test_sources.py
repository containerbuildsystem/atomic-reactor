"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor import source
from atomic_reactor.tasks import sources


class TestSourceBuildTaskParams:
    """Tests for the SourceBuildTaskParams class."""

    def test_source_property(self, tmpdir):
        params = sources.SourceBuildTaskParams(
            # build_dir has to be an existing directory because DummySource creates a subdirectory
            #   as soon as an instance is created
            build_dir=str(tmpdir),
            context_dir="/context",
            config_file="config.yaml",
            user_params={},
        )
        src = params.source

        assert isinstance(src, source.DummySource)
        assert src.workdir == str(tmpdir)
