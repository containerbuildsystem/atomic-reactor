"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.constants import INSPECT_CONFIG
from atomic_reactor.plugin import PreBuildPluginsRunner  # noqa
from atomic_reactor.plugins.pre_distribution_scope import (DistributionScopePlugin,
                                                           DisallowedDistributionScope)
from flexmock import flexmock
import logging
import os
import pytest


class TestDistributionScope(object):
    def instantiate_plugin(self, tmpdir, parent_labels, current_scope, base_from_scratch=False):
        workflow = flexmock()
        setattr(workflow, 'builder', flexmock())
        filename = os.path.join(str(tmpdir), 'Dockerfile')
        with open(filename, 'wt') as df:
            df.write('FROM scratch\n')
            if current_scope:
                df.write('LABEL distribution-scope={}\n'.format(current_scope))

        setattr(workflow.builder, 'df_path', filename)

        setattr(workflow.builder, 'base_image_inspect', {})
        if not base_from_scratch:
            setattr(workflow.builder, 'base_image_inspect', {
                INSPECT_CONFIG: {
                    'Labels': parent_labels,
                }
            })
        setattr(workflow.builder, 'base_from_scratch', base_from_scratch)

        plugin = DistributionScopePlugin(None, workflow)
        plugin.log = logging.getLogger('plugin')
        return plugin

    @pytest.mark.parametrize('base_from_scratch', [True, False])
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
    def test_distribution_scope_allowed(self, tmpdir, base_from_scratch, parent_scope,
                                        current_scope, allowed, caplog):
        caplog.set_level(logging.ERROR, logger='atomic_reactor')
        plugin = self.instantiate_plugin(tmpdir,
                                         {'distribution-scope': parent_scope},
                                         current_scope,
                                         base_from_scratch=base_from_scratch)
        if base_from_scratch:
            allowed = True
        if allowed:
            with caplog.at_level(logging.ERROR):
                plugin.run()

            # No errors logged
            assert not caplog.records
            if base_from_scratch:
                "no distribution scope set for" in caplog.text
        else:
            with pytest.raises(DisallowedDistributionScope):
                plugin.run()

            # Should log something at ERROR
            assert caplog.records

    @pytest.mark.parametrize('current_scope', [None, 'private'])
    def test_imported_parent_distribution_scope(self, tmpdir, caplog, current_scope):
        plugin = self.instantiate_plugin(tmpdir, None, current_scope)
        with caplog.at_level(logging.ERROR, logger='atomic_reactor'):
            plugin.run()

        # No errors logged
        assert not caplog.records

    @pytest.mark.parametrize('current_scope', [None, 'private'])
    def test_invalid_parent_distribution_scope(self, tmpdir, caplog, current_scope):
        plugin = self.instantiate_plugin(tmpdir,
                                         {'distribution-scope': 'invalid-choice'},
                                         current_scope)
        with caplog.at_level(logging.WARNING, logger='atomic_reactor'):
            plugin.run()

            if current_scope:
                # Warning logged (if we get as far as checking parent scope)
                assert 'invalid label' in caplog.text

    @pytest.mark.parametrize('parent_scope', [None, 'private'])
    def test_invalid_current_distribution_scope(self, tmpdir, caplog, parent_scope):
        plugin = self.instantiate_plugin(tmpdir,
                                         {'distribution-scope': parent_scope},
                                         'invalid-choice')
        with caplog.at_level(logging.WARNING, logger='atomic_reactor'):
            plugin.run()

            # Warning logged
            assert 'invalid label' in caplog.text
