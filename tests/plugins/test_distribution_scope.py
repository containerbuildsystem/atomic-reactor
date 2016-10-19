"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.constants import INSPECT_CONFIG
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.plugins.pre_distribution_scope import (DistributionScopePlugin,
                                                           DisallowedDistributionScope)
from flexmock import flexmock
import logging
import os
import pytest


class TestDistributionScope(object):
    def create_dockerfile(self, tmpdir, current_scope):

        return filename

    def instantiate_plugin(self, tmpdir, parent_labels, current_scope):
        workflow = flexmock()
        setattr(workflow, 'builder', flexmock())
        filename = os.path.join(str(tmpdir), 'Dockerfile')
        with open(filename, 'wt') as df:
            df.write('FROM scratch\n')
            if current_scope:
                df.write('LABEL distribution-scope={}\n'.format(current_scope))

        setattr(workflow.builder, 'df_path', filename)
        setattr(workflow, 'base_image_inspect', {
            INSPECT_CONFIG: {
                'Labels': parent_labels,
            }
        })

        plugin = DistributionScopePlugin(None, workflow)
        plugin.log = logging.getLogger('plugin')
        return plugin

    @pytest.mark.parametrize(('parent_scope', 'current_scope', 'allowed'), [
        (None, None, True),
        (None, 'private', True),
        (None, 'authoritative-source-only', True),
        (None, 'restricted', True),
        (None, 'public', True),
        ('private', None, True),
        ('private', 'private', True),
        ('private', 'authoritative-source-only', False),
        ('private', 'restricted', False),
        ('private', 'public', False),
    ])
    def test_distribution_scope_allowed(self, tmpdir, parent_scope,
                                        current_scope, allowed, caplog):
        plugin = self.instantiate_plugin(tmpdir,
                                         {'distribution-scope': parent_scope},
                                         current_scope)
        if allowed:
            with caplog.atLevel(logging.ERROR):
                plugin.run()

            # No errors logged
            assert not caplog.records()
        else:
            with pytest.raises(DisallowedDistributionScope):
                plugin.run()

            # Should log something at ERROR
            assert caplog.records()

    @pytest.mark.parametrize('current_scope', [None, 'private'])
    def test_imported_parent_distribution_scope(self, tmpdir, caplog, current_scope):
        plugin = self.instantiate_plugin(tmpdir, None, current_scope)
        with caplog.atLevel(logging.ERROR):
            plugin.run()

        # No errors logged
        assert not caplog.records()

    @pytest.mark.parametrize('current_scope', [None, 'private'])
    def test_invalid_parent_distribution_scope(self, tmpdir, caplog, current_scope):
        plugin = self.instantiate_plugin(tmpdir,
                                         {'distribution-scope': 'invalid-choice'},
                                         current_scope)
        with caplog.atLevel(logging.WARNING):
            plugin.run()

            if current_scope:
                # Warning logged (if we get as far as checking parent scope)
                assert 'invalid label' in caplog.text()

    @pytest.mark.parametrize('parent_scope', [None, 'private'])
    def test_invalid_current_distribution_scope(self, tmpdir, caplog, parent_scope):
        plugin = self.instantiate_plugin(tmpdir,
                                         {'distribution-scope': parent_scope},
                                         'invalid-choice')
        with caplog.atLevel(logging.WARNING):
            plugin.run()

            # Warning logged
            assert 'invalid label' in caplog.text()
