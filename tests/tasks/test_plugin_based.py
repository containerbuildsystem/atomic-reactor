"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import pytest

from osbs.exceptions import OsbsValidationException

from atomic_reactor.tasks import plugin_based


class TestPluginsDef:
    """Tests for the PluginsDef class."""

    def test_create_valid(self):
        plugins = plugin_based.PluginsDef(build=[{"name": "some_plugin"}])
        assert plugins.prebuild == []
        assert plugins.build == [{"name": "some_plugin"}]
        assert plugins.prepublish == []
        assert plugins.postbuild == []
        assert plugins.exit == []

    def test_create_invalid(self):
        with pytest.raises(OsbsValidationException, match="1 is not of type 'boolean'"):
            plugin_based.PluginsDef(prebuild=[{"name": "some_plugin", "required": 1}])
